import asyncio
import logging
import multiprocessing as mp
import pathlib
import re
import sys
import time
from decimal import Decimal
from multiprocessing.connection import Connection
from typing import Callable, Dict, List, Tuple

import dateutil.parser
from funding_service_client.async_fs_client import FSClient
from funding_service_client.constants import (WORKER_STATUS_FINISH,
                                              WORKER_STATUS_PROC)
from funding_service_client.fs_exception import InsufficientBalanceError

from src.common import Config, Exchange, to_decimal_or_none
from src.exchange.exchange_data_type import Side, TradeType
from src.exchange.ftx.ftx_client import FtxExchange
from src.exchange.ftx.ftx_data_type import (Ftx_EWMA_InterestRate,
                                            FtxCollateralWeight,
                                            FtxCollateralWeightMessage,
                                            FtxEntryPriceRequestMessage,
                                            FtxEntryPriceResponseMessage,
                                            FtxFeeRate, FtxFeeRateMessage,
                                            FtxFundOpenFilledMessage,
                                            FtxFundRequestMessage,
                                            FtxHedgePair, FtxHedgePairSummary,
                                            FtxInterestRateMessage,
                                            FtxLeverageInfo,
                                            FtxLeverageMessage,
                                            FtxOrderMessage, FtxOrderStatus,
                                            FtxOrderType, FtxTradingRule,
                                            FtxTradingRuleMessage,
                                            OpenCloseInfo)
from src.script.sub_process import run_sub_process
from src.util.fund_manager import FundManager
from src.util.rate_limit import RateLimiter
from src.util.slack import SlackWrappedLogger


class MainProcess:
    MARKET_STATUS_POLLING_INTERVAL = 300
    INTEREST_RATE_POLLING_INTERVAL = 3600
    FEE_RATE_POLLING_INTERVAL = 300
    COLLATERAL_WEIGHT_POLLING_INTERVAL = 300
    ACCOUNT_INFO_POLLING_INTERVAL = 5
    RELEASE_DEAD_SUB_PROCESS_INTERVAL = 300
    LOG_SUMMARY_INTERVAL = 3600
    FUNDING_SERVICE_INTERVAL = 600

    def __init__(self, config: Config):
        self.config: Config = config
        self.logger = self._init_get_logger()
        if config.exchange == Exchange.FTX:
            self.exchange = FtxExchange(
                config.api_key, config.api_secret, config.subaccount_name,
            )
            self.trading_rules: Dict[str, FtxTradingRule] = {}
            self.hedge_pairs: Dict[str, FtxHedgePair] = {}
            self.ewma_interest_rate = Ftx_EWMA_InterestRate(
                lookback_days=config.interest_rate_lookback_days,
                ignore=config.ignore_interest_rate,
            )
            self.fee_rate = FtxFeeRate()
            self.collateral_weights: Dict[str, FtxCollateralWeight] = {}
            self.leverage_info = FtxLeverageInfo()

            # log summary
            self._entry_prices: Dict[str, Decimal] = {}  # market: price
            self._receive_entry_price_events: Dict[
                str, asyncio.Event
            ] = {}  # market: Event

            # params initializer, to notify sub process all params are ready
            self._trading_rules_ready_event = asyncio.Event()
            self._interest_rate_ready_event = asyncio.Event()
            self._fee_rate_ready_event = asyncio.Event()
            self._collateral_weights_ready_event = asyncio.Event()
            self._account_info_ready_event = asyncio.Event()

            # Sub processes
            self._hedge_pair_initialized_cond = asyncio.Condition()
            self._loop = asyncio.get_event_loop()
            self._connections: Dict[str, Tuple[Connection, Connection]] = {}
            self._sub_processes: Dict[str, mp.Process] = {}
            self._sub_process_notify_events: Dict[str, asyncio.Event] = {}

            # tasks
            self._market_status_polling_task: asyncio.Task = None
            self._interest_rate_polling_task: asyncio.Task = None
            self._fee_rate_polling_task: asyncio.Task = None
            self._collateral_weight_polling_task: asyncio.Task = None
            self._spawn_sub_processes_task: asyncio.Task = None
            self._sub_process_listen_tasks: Dict[str, asyncio.Task] = {}
            self._start_ws_task: asyncio.Task = None
            self._listen_ws_orders_task: asyncio.Task = None
            self._account_info_polling_task: asyncio.Task = None
            self._release_dead_sub_process_loop_task: asyncio.Task = None
            self._log_summary_polling_task: asyncio.Task = None
            self._apply_funding_service_task: asyncio.Task = None

            # websocket
            self.exchange.ws_register_order_channel()

            # fund manager
            self.fund_manager = FundManager(leverage_limit=config.leverage_limit)

            # rate limit
            self.rate_limiter_manager = mp.Manager()
            interval = config.rate_limit_config.interval
            limit = config.rate_limit_config.limit
            self.rate_limiter = RateLimiter(self.rate_limiter_manager, interval, limit)

            # funding service
            self._fs_client: FSClient = None
            self._deposit_history_cache: List[dict] = []
            self._withdraw_history_cache: List[dict] = []

    def _init_get_logger(self):
        log = self.config.log
        level = logging.getLevelName(log["level"].upper())
        fmt = log["fmt"]
        datefmt = log["datefmt"]
        formatter = logging.Formatter(fmt, datefmt)
        handlers = []
        if log["to_console"]:
            ch = logging.StreamHandler()
            ch.setFormatter(formatter)
            ch.set_name("stream_formatter")
            handlers.append(ch)
        if log["to_file"]:
            path = pathlib.Path(log["file_path"])
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
            fh = logging.FileHandler(log["file_path"], encoding="utf-8")
            fh.setFormatter(formatter)
            fh.set_name("file_handler")
            handlers.append(fh)
        logging.basicConfig(level=level, handlers=handlers)
        logger = logging.getLogger()
        logger = SlackWrappedLogger(
            logger,
            {
                "auth_token": self.config.slack_config.auth_token,
                "info_channel": self.config.slack_config.summary_channel,
                "alert_channel": self.config.slack_config.alert_channel,
            },
        )
        return logger

    @property
    def interest_rate(self) -> Decimal:
        return self.ewma_interest_rate.hourly_rate

    @property
    def status_dict(self) -> Dict[str, bool]:
        return {
            "trading_rule_initialized": len(self.trading_rules) > 0,
            "hedge_pair_initialized": len(self.hedge_pairs) > 0,
            "interest_rate_initialized": self.interest_rate is not None,
            "taker_fee_rate_initialized": self.fee_rate.taker_fee_rate is not None,
            "collateral_weight_initialized": len(self.collateral_weights) > 0,
        }

    @property
    def ready(self) -> bool:
        return all(self.status_dict.values())

    def start_network(self):
        if self._market_status_polling_task is None:
            self._market_status_polling_task = asyncio.create_task(
                self._market_status_polling_loop()
            )
        if self._interest_rate_polling_task is None:
            self._interest_rate_polling_task = asyncio.create_task(
                self._interest_rate_polling_loop()
            )
        if self._fee_rate_polling_task is None:
            self._fee_rate_polling_task = asyncio.create_task(
                self._fee_rate_polling_loop()
            )
        if self._collateral_weight_polling_task is None:
            self._collateral_weight_polling_task = asyncio.create_task(
                self._collateral_weight_polling_loop()
            )
        if self._spawn_sub_processes_task is None:
            self._spawn_sub_processes_task = asyncio.create_task(
                self._spawn_sub_processes()
            )
        if self._start_ws_task is None:
            self._start_ws_task = asyncio.create_task(self.exchange.ws_start_network())
        if self._account_info_polling_task is None:
            self._account_info_polling_task = asyncio.create_task(
                self._account_info_polling_loop()
            )
        if self._listen_ws_orders_task is None:
            self._listen_ws_orders_task = asyncio.create_task(self._listen_ws_orders())
        if self._release_dead_sub_process_loop_task is None:
            self._release_dead_sub_process_loop_task = asyncio.create_task(
                self._release_dead_sub_process_loop()
            )
        if self._log_summary_polling_task is None:
            self._log_summary_polling_task = asyncio.create_task(
                self._log_summary_polling_loop()
            )
        if (
            self.config.funding_service_config.enable
            and self._apply_funding_service_task is None
        ):
            self._apply_funding_service_task = asyncio.create_task(
                self._apply_funding_service_loop()
            )

    def stop_network(self):
        if self._market_status_polling_task is not None:
            self._market_status_polling_task.cancel()
            self._market_status_polling_task = None
        if self._interest_rate_polling_task is not None:
            self._interest_rate_polling_task.cancel()
            self._interest_rate_polling_task = None
        if self._fee_rate_polling_task is not None:
            self._fee_rate_polling_task.cancel()
            self._fee_rate_polling_task = None
        if self._collateral_weight_polling_task is not None:
            self._collateral_weight_polling_task.cancel()
            self._collateral_weight_polling_task = None
        if self._spawn_sub_processes_task is not None:
            self._spawn_sub_processes_task.cancel()
            self._spawn_sub_processes_task = None
        if self._start_ws_task is not None:
            self._start_ws_task.cancel()
            self._start_ws_task = None
        if self._account_info_polling_task is not None:
            self._account_info_polling_task.cancel()
            self._account_info_polling_task = None
        if self._listen_ws_orders_task is not None:
            self._listen_ws_orders_task.cancel()
            self._listen_ws_orders_task = None
        if self._release_dead_sub_process_loop_task is not None:
            self._release_dead_sub_process_loop_task.cancel()
            self._release_dead_sub_process_loop_task = None
        if self._log_summary_polling_task is not None:
            self._log_summary_polling_task.cancel()
            self._log_summary_polling_task = None
        if self._apply_funding_service_task is not None:
            self._apply_funding_service_task.cancel()
            self._apply_funding_service_task = None
        self._stop_all_sub_process_listen_tasks()
        self._stop_all_sub_processes()

    async def _market_status_polling_loop(self):
        """Handle the market infomations. Combined the bollowing tasks to make only one request.
        1. update TradingRule
        2. uddate HedgePair
        """
        while True:
            try:
                markets = await self.exchange.get_markets()
                self._update_trading_rule(markets)
                await self._update_hedge_pair(markets)
                await asyncio.sleep(self.MARKET_STATUS_POLLING_INTERVAL)
            except asyncio.CancelledError:
                raise
            except BlockingIOError:
                self.logger.error(
                    "System exit", exc_info=True, slack=self.config.slack_config.enable,
                )
                sys.exit(1)
            except Exception:
                self.logger.error(
                    "Unexpected error while fetching market status.",
                    exc_info=True,
                    slack=self.config.slack_config.enable,
                )
                await asyncio.sleep(10)

    def _update_trading_rule(self, market_infos: List[dict]):
        trading_rules = {}
        for market in market_infos:
            symbol = market["name"]
            min_order_size = Decimal(str(market["sizeIncrement"]))
            price_tick = Decimal(str(market["priceIncrement"]))
            trading_rules[symbol] = FtxTradingRule(symbol, min_order_size, price_tick)
        self.trading_rules.update(trading_rules)
        self._trading_rules_ready_event.set()
        for coin, (conn, _) in self._connections.items():
            spot = FtxHedgePair.coin_to_spot(coin)
            future = FtxHedgePair.coin_to_future(coin, self.config.season)
            if self.trading_rules.get(spot):
                conn.send(FtxTradingRuleMessage(self.trading_rules[spot]))
            if self.trading_rules.get(future):
                conn.send(FtxTradingRuleMessage(self.trading_rules[future]))

    async def _update_hedge_pair(self, market_infos: List[dict]):
        symbol_set = set([info["name"] for info in market_infos if info["enabled"]])
        regex = re.compile(f"[0-9A-Z]+-{self.config.season}")
        hedge_pairs: Dict[str, FtxHedgePair] = {}
        for symbol in symbol_set:
            if (
                regex.match(symbol)
                and FtxHedgePair.future_to_spot(symbol) in symbol_set
            ):
                coin = FtxHedgePair.future_to_coin(symbol)
                hedge_pairs[coin] = FtxHedgePair.from_future(symbol)

        # hedge pairs that have position but not in whitelist should be set to close only mode
        coins_that_have_position = await self._get_coins_that_have_position(symbol_set)

        # hedge pairs that have low daily trading volume (illiquid pair) should be added to blacklist
        market_volume_map: Dict[str, float] = {
            info["name"]: info["volumeUsd24h"] for info in market_infos
        }
        low_volume_coins: List[str] = []
        for coin, pair in hedge_pairs.items():
            if (
                market_volume_map.get(pair.spot) is not None
                and market_volume_map[pair.spot] < self.config.min_volume_usd_24h
            ):
                low_volume_coins.append(coin)
                continue
            if (
                market_volume_map.get(pair.future) is not None
                and market_volume_map[pair.future] < self.config.min_volume_usd_24h
            ):
                low_volume_coins.append(coin)
                continue

        # blacklist coins with low collateral weight
        await self._collateral_weights_ready_event.wait()
        low_weight_coins: List[str] = []
        for coin, pair in hedge_pairs.items():
            if (
                self.collateral_weights.get(coin) is None
                or self.collateral_weights[coin].weight < 0.1
            ):
                low_weight_coins.append(coin)

        # handle whitelist
        if len(self.config.whitelist) == 0:
            self.hedge_pairs.update(hedge_pairs)
        else:
            for coin in self.config.whitelist:
                if hedge_pairs.get(coin):
                    self.hedge_pairs[coin] = hedge_pairs[coin]
                else:
                    self.logger.warning(
                        f"{coin} in whitelist is not found in the market"
                    )
            for coin in coins_that_have_position:
                if coin not in self.config.whitelist:
                    self.hedge_pairs[coin] = FtxHedgePair.from_coin(
                        coin, self.config.season, TradeType.CLOSE_ONLY
                    )

        # handle blacklist
        for coin in (
            set(self.config.blacklist).union(low_volume_coins).union(low_weight_coins)
        ):
            if self.hedge_pairs.get(coin):
                if coin in coins_that_have_position:
                    self.hedge_pairs[coin].trade_type = TradeType.CLOSE_ONLY
                else:
                    del self.hedge_pairs[coin]

        async with self._hedge_pair_initialized_cond:
            self._hedge_pair_initialized_cond.notify_all()

    async def _get_coins_that_have_position(self, symbol_set: set) -> List[str]:
        await self._trading_rules_ready_event.wait()
        balances = await self.exchange.get_balances()
        balance_map = {
            b["coin"]: Decimal(str(b["total"]))
            for b in balances
            if FtxHedgePair.coin_to_spot(b["coin"]) in symbol_set
        }
        positions = await self.exchange.get_positions()
        coins = []
        for position in positions:
            future = position["future"]
            coin = FtxHedgePair.future_to_coin(future)
            spot = FtxHedgePair.future_to_spot(future)
            future_new_size = Decimal(str(position["netSize"]))
            if self.trading_rules.get(spot) and self.trading_rules.get(future):
                min_order_size = max(
                    self.trading_rules[spot].min_order_size,
                    self.trading_rules[future].min_order_size,
                )
                if future_new_size > -min_order_size:
                    continue
                if balance_map.get(coin, Decimal(0)) >= min_order_size:
                    coins.append(coin)
        return coins

    async def _interest_rate_polling_loop(self):
        while True:
            try:
                et = time.time()
                ewma = self.ewma_interest_rate.last_ewma
                if ewma is None:
                    st = et - self.ewma_interest_rate.lookback_days * 24 * 3600
                else:
                    st = self.ewma_interest_rate.last_timestamp + 1
                rate_info = await self.exchange.get_full_spot_margin_history(st, et)
                if len(rate_info) == 0:
                    await asyncio.sleep(self.INTEREST_RATE_POLLING_INTERVAL)
                    continue
                for info in rate_info:
                    rate = Decimal(str(info["rate"]))
                    ewma = (
                        self.ewma_interest_rate.lambda_ * rate
                        + (1 - self.ewma_interest_rate.lambda_) * ewma
                        if ewma
                        else rate
                    )
                self.ewma_interest_rate.last_ewma = ewma
                self.ewma_interest_rate.last_timestamp = dateutil.parser.parse(
                    rate_info[-1]["time"]
                ).timestamp()
                self._interest_rate_ready_event.set()
                for (conn, _) in self._connections.values():
                    conn.send(FtxInterestRateMessage(self.ewma_interest_rate))
                await asyncio.sleep(self.INTEREST_RATE_POLLING_INTERVAL)
            except asyncio.CancelledError:
                raise
            except BlockingIOError:
                self.logger.error(
                    "System exit", exc_info=True, slack=self.config.slack_config.enable,
                )
                sys.exit(1)
            except Exception:
                self.logger.error(
                    "Unexpected error while fetching USD interest rate.",
                    exc_info=True,
                    slack=self.config.slack_config.enable,
                )
                await asyncio.sleep(5)

    async def _fee_rate_polling_loop(self):
        while True:
            try:
                account = await self.exchange.get_account()
                self.fee_rate.maker_fee_rate = Decimal(str(account["makerFee"]))
                self.fee_rate.taker_fee_rate = Decimal(str(account["takerFee"]))
                self.ewma_interest_rate.set_taker_fee_rate(self.fee_rate.taker_fee_rate)
                self._fee_rate_ready_event.set()
                for (conn, _) in self._connections.values():
                    conn.send(FtxFeeRateMessage(self.fee_rate))
                await asyncio.sleep(self.FEE_RATE_POLLING_INTERVAL)
            except asyncio.CancelledError:
                raise
            except BlockingIOError:
                self.logger.error(
                    "System exit", exc_info=True, slack=self.config.slack_config.enable,
                )
                sys.exit(1)
            except Exception:
                self.logger.error(
                    "Unexpected error while fetching account fee rate.",
                    exc_info=True,
                    slack=self.config.slack_config.enable,
                )
                await asyncio.sleep(5)

    async def _collateral_weight_polling_loop(self):
        while True:
            try:
                coin_infos = await self.exchange.get_coins()
                for info in coin_infos:
                    coin = info["id"]
                    weight = Decimal(str(info["collateralWeight"]))
                    self.collateral_weights[coin] = FtxCollateralWeight(
                        coin=coin, weight=weight
                    )
                    if self._connections.get(coin):
                        self._connections[coin][0].send(
                            FtxCollateralWeightMessage(self.collateral_weights[coin])
                        )
                self._collateral_weights_ready_event.set()
                await asyncio.sleep(self.COLLATERAL_WEIGHT_POLLING_INTERVAL)
            except asyncio.CancelledError:
                raise
            except BlockingIOError:
                self.logger.error(
                    "System exit", exc_info=True, slack=self.config.slack_config.enable,
                )
                sys.exit(1)
            except Exception:
                self.logger.error(
                    "Unexpected error while fetching coin collateral weights.",
                    exc_info=True,
                    slack=self.config.slack_config.enable,
                )
                await asyncio.sleep(5)

    def _update_account_info(self, account_info: dict):
        account_value = Decimal(str(account_info["totalAccountValue"]))
        position_value = Decimal(str(account_info["totalPositionSize"]))
        current_leverage = position_value / account_value
        self.leverage_info = FtxLeverageInfo(
            max_leverage=Decimal(str(account_info["leverage"])),
            account_value=account_value,
            position_value=position_value,
            current_leverage=current_leverage,
        )
        self._account_info_ready_event.set()
        for (conn, _) in self._connections.values():
            conn.send(FtxLeverageMessage(self.leverage_info))

    async def _account_info_polling_loop(self):
        while True:
            try:
                account_info = await self.exchange.get_account()
                self._update_account_info(account_info)
                await self.fund_manager.update_account_state(account_info)
                await asyncio.sleep(self.ACCOUNT_INFO_POLLING_INTERVAL)
            except asyncio.CancelledError:
                raise
            except BlockingIOError:
                self.logger.error(
                    "System exit", exc_info=True, slack=self.config.slack_config.enable,
                )
                sys.exit(1)
            except Exception:
                self.logger.error(
                    "Unexpected error while fetching account info.",
                    exc_info=True,
                    slack=self.config.slack_config.enable,
                )
                await asyncio.sleep(5)

    async def _spawn_sub_processes(self):
        while True:
            try:
                async with self._hedge_pair_initialized_cond:
                    await self._hedge_pair_initialized_cond.wait()
                await self._trading_rules_ready_event.wait()
                self.logger.debug("trading rules ready!")
                await self._interest_rate_ready_event.wait()
                self.logger.debug("interest rate ready!")
                await self._fee_rate_ready_event.wait()
                self.logger.debug("fee rate ready!")
                await self._collateral_weights_ready_event.wait()
                self.logger.debug("collateral weights ready!")
                await self._account_info_ready_event.wait()
                self.logger.debug("account info ready!")

                for coin, hedge_pair in self.hedge_pairs.items():
                    if self._sub_processes.get(coin) is None:
                        # build pipe connection
                        conn1, conn2 = mp.Pipe(duplex=True)
                        self._connections[hedge_pair.coin] = (conn1, conn2)
                        self._sub_process_notify_events[coin] = asyncio.Event()
                        # spawn sub process
                        sub_process = mp.Process(
                            target=run_sub_process,
                            args=(hedge_pair, self.config, conn2, self.rate_limiter),
                            daemon=True,
                        )
                        sub_process.start()
                        self._sub_processes[coin] = sub_process
                        # create sub process listening task
                        self._sub_process_listen_tasks[coin] = asyncio.create_task(
                            self._listen_sub_process_msg(coin)
                        )

                        # notify params
                        if self.trading_rules.get(hedge_pair.spot):
                            conn1.send(
                                FtxTradingRuleMessage(
                                    self.trading_rules[hedge_pair.spot]
                                )
                            )
                        if self.trading_rules.get(hedge_pair.future):
                            conn1.send(
                                FtxTradingRuleMessage(
                                    self.trading_rules[hedge_pair.future]
                                )
                            )
                        conn1.send(
                            FtxInterestRateMessage(
                                ewma_interest_rate=self.ewma_interest_rate
                            )
                        )
                        conn1.send(FtxFeeRateMessage(fee_rate=self.fee_rate))
                        if self.collateral_weights.get(coin):
                            conn1.send(
                                FtxCollateralWeightMessage(
                                    collateral_weight=self.collateral_weights[coin]
                                )
                            )
                        conn1.send(FtxLeverageMessage(leverage=self.leverage_info))

            except asyncio.CancelledError:
                raise
            except BlockingIOError:
                self.logger.error(
                    "System exit", exc_info=True, slack=self.config.slack_config.enable,
                )
                sys.exit(1)
            except Exception as e:
                self.logger.error(
                    f"Unexpected error while spawn new sub process. {e}",
                    exc_info=True,
                    slack=self.config.slack_config.enable,
                )

    async def _release_dead_sub_process_loop(self):
        while True:
            try:
                for coin, process in list(self._sub_processes.items()):
                    if not process.is_alive():
                        # release process resource
                        process.join()
                        process.close()
                        del self._sub_processes[coin]
                        # release PIPE connection resource
                        self._connections[coin][0].close()
                        self._connections[coin][1].close()
                        del self._connections[coin]
                        self.logger.info(f"Close {coin} sub process")
                await asyncio.sleep(self.RELEASE_DEAD_SUB_PROCESS_INTERVAL)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.error(
                    "Unexpected error while release dead sub process.",
                    exc_info=True,
                    slack=self.config.slack_config.enable,
                )

    def _stop_all_sub_processes(self):
        for coin, process in self._sub_processes.items():
            # release process resource
            process.terminate()
            process.join()
            process.close()
            del self._sub_processes[coin]
            # release PIPE connection resource
            self._connections[coin][0].close()
            self._connections[coin][1].close()
            del self._connections[coin]
            self.logger.info(f"Close {coin} sub process")

    async def _listen_sub_process_msg(self, coin: str):
        conn = self._connections[coin][0]
        self._loop.add_reader(conn.fileno(), self._sub_process_notify_events[coin].set)
        while True:
            try:
                if not conn.poll():
                    await self._sub_process_notify_events[coin].wait()
                msg = conn.recv()
                self.logger.debug(f"Get msg from {coin} child process: {msg}")
                if type(msg) is FtxFundRequestMessage:
                    response = await self.fund_manager.request_for_budget(msg)
                    conn.send(response)
                elif type(msg) is FtxFundOpenFilledMessage:
                    await self.fund_manager.handle_open_order_filled(msg)
                elif type(msg) is FtxEntryPriceResponseMessage:
                    market = msg.market
                    if self._receive_entry_price_events.get(market) is None:
                        self._receive_entry_price_events[market] = asyncio.Event()
                    self._entry_prices[market] = msg.entry_price
                    self._receive_entry_price_events[market].set()
                self._sub_process_notify_events[coin].clear()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.error(
                    "Unexpected error while listen to sub process message.",
                    exc_info=True,
                    slack=self.config.slack_config.enable,
                )
                await asyncio.sleep(5)

    async def _stop_all_sub_process_listen_tasks(self):
        for task in self._sub_process_listen_tasks.values():
            task.cancel()

    async def _listen_ws_orders(self):
        while True:
            try:
                data = await self.exchange.orders.get()
                order_msg = FtxOrderMessage(
                    id=str(data["id"]),
                    market=data["market"],
                    type=FtxOrderType.LIMIT
                    if data["type"] == "limit"
                    else FtxOrderType.MARKET,
                    side=Side.BUY if data["side"] == "buy" else Side.SELL,
                    size=Decimal(str(data["size"])),
                    price=Decimal(str(data["price"]))
                    if isinstance(data["price"], (float, int))
                    else data["price"],
                    status=FtxOrderStatus.str_entry(data["status"]),
                    filled_size=Decimal(str(data["filledSize"])),
                    avg_fill_price=Decimal(str(data["avgFillPrice"]))
                    if data["avgFillPrice"]
                    else None,
                    create_timestamp=dateutil.parser.parse(
                        data["createdAt"]
                    ).timestamp(),
                )
                if FtxHedgePair.is_spot(order_msg.market):
                    coin = FtxHedgePair.spot_to_coin(order_msg.market)
                    if self._connections.get(coin):
                        conn = self._connections[coin][0]
                        conn.send(order_msg)
                elif FtxHedgePair.is_future(order_msg.market, self.config.season):
                    coin = FtxHedgePair.future_to_coin(order_msg.market)
                    if self._connections.get(coin):
                        conn = self._connections[coin][0]
                        conn.send(order_msg)
                else:
                    self.logger.warning(f"Get unknown order msg: {order_msg}")
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.error(
                    "Unexpected error while listen to ws orders.",
                    exc_info=True,
                    slack=self.config.slack_config.enable,
                )
                await asyncio.sleep(5)

    async def _log_summary_polling_loop(self):
        await asyncio.sleep(60)  # wait for entry price update
        try:
            while True:
                try:
                    await self._trading_rules_ready_event.wait()
                    username_task = self.exchange.get_username()
                    account_task = self.exchange.get_account()
                    balances_task = self.exchange.get_balances()
                    username, account, balances = await asyncio.gather(
                        username_task, account_task, balances_task
                    )
                    positions = account.pop("positions")
                    account_value = to_decimal_or_none(account["totalAccountValue"])
                    collateral_supply = to_decimal_or_none(account["collateral"])
                    free_collateral = to_decimal_or_none(account["freeCollateral"])
                    position_value = to_decimal_or_none(account["totalPositionSize"])
                    leverage = position_value / account_value
                    summarys: Dict[str, FtxHedgePairSummary] = {}

                    # loop balances
                    usd_size = Decimal(0)
                    account_usd_value = Decimal(0)
                    for balance in balances:
                        coin = balance["coin"]
                        spot = FtxHedgePair.coin_to_spot(coin)
                        total = to_decimal_or_none(balance["total"])
                        usd_value = to_decimal_or_none(balance["usdValue"])
                        account_usd_value += usd_value
                        if coin == "USD":
                            usd_size = total
                        else:
                            if total != 0:
                                summary = FtxHedgePairSummary(
                                    FtxHedgePair.from_coin(coin, self.config.season),
                                    spot_size=total,
                                    spot_usd_value=usd_value,
                                    spot_price_tick=self.trading_rules[spot].price_tick,
                                )
                                summarys[coin] = summary

                    # loop positions
                    for position in positions:
                        future = position["future"]
                        if not FtxHedgePair.is_future(future, self.config.season):
                            continue
                        future_size = to_decimal_or_none(position["netSize"])
                        coin = FtxHedgePair.future_to_coin(future)
                        if summarys.get(coin):
                            summarys[coin].future_size = future_size
                            summarys[coin].future_price_tick = self.trading_rules[
                                future
                            ].price_tick
                        else:
                            if future_size != 0:
                                summary = FtxHedgePairSummary(
                                    FtxHedgePair.from_future(future),
                                    future_size=future_size,
                                    future_price_tick=self.trading_rules[
                                        future
                                    ].price_tick,
                                )

                    # request entry price from sub process
                    for coin, summary in summarys.items():
                        if self._connections.get(coin) is None:
                            continue
                        spot = summary.hedge_pair.spot
                        future = summary.hedge_pair.future
                        conn = self._connections[coin][0]
                        # update spot entry price
                        conn.send(FtxEntryPriceRequestMessage(spot))
                        if self._receive_entry_price_events.get(spot) is None:
                            self._receive_entry_price_events[spot] = asyncio.Event()
                        try:
                            await asyncio.wait_for(
                                self._receive_entry_price_events[spot].wait(), 1
                            )
                        except asyncio.TimeoutError:
                            continue
                        else:
                            summary.spot_entry_price = self._entry_prices[spot]
                        finally:
                            self._receive_entry_price_events[spot].clear()
                        # update future entry price
                        conn.send(FtxEntryPriceRequestMessage(future))
                        if self._receive_entry_price_events.get(future) is None:
                            self._receive_entry_price_events[future] = asyncio.Event()
                        try:
                            await asyncio.wait_for(
                                self._receive_entry_price_events[future].wait(), 1
                            )
                        except asyncio.TimeoutError:
                            continue
                        else:
                            summary.future_entry_price = self._entry_prices[future]
                        finally:
                            self._receive_entry_price_events[future].clear()

                    # create summary text
                    text = f"{username}\n"
                    text += f"Total USD value: ${account_usd_value:,.0f}\n"
                    text += f"Collateral supply: ${collateral_supply:,.0f}\n"
                    text += f"Free collateral: $ {free_collateral:,.0f}\n"
                    text += f"Leverage: {leverage:.2f}x\n"
                    text += f">USD ${usd_size:,.0f}\n"
                    for summary in sorted(summarys.values(), reverse=True):
                        text += f">{summary}\n"

                    # get latest log summary interval fills
                    now = time.time()
                    end_ts = (
                        now // self.LOG_SUMMARY_INTERVAL * self.LOG_SUMMARY_INTERVAL
                    )
                    start_ts = end_ts - self.LOG_SUMMARY_INTERVAL
                    fills = await self.exchange.get_fills(start_ts, end_ts)

                    info_map: Dict[str, OpenCloseInfo] = {}
                    for fill in fills:
                        market = fill["market"]
                        if FtxHedgePair.is_spot(market):
                            spot = market
                            coin = FtxHedgePair.spot_to_coin(spot)
                            future = FtxHedgePair.spot_to_future(
                                spot, self.config.season
                            )
                        elif FtxHedgePair.is_future(market, self.config.season):
                            future = market
                            coin = FtxHedgePair.future_to_coin(future)
                            spot = FtxHedgePair.future_to_spot(future)
                        if info_map.get(coin) is None:
                            info_map[coin] = OpenCloseInfo(
                                hedge_pair=FtxHedgePair(coin, spot, future),
                            )
                        info = info_map[coin]
                        info.fill_entry(fill)

                    if len(info_map) > 0:
                        text += "Open and close info during last hour:\n"
                        for info in info_map.values():
                            future_trading_rule = self.trading_rules.get(
                                info.hedge_pair.future
                            )
                            spot_trading_rule = self.trading_rules.get(
                                info.hedge_pair.spot
                            )
                            text += f"{info.hedge_pair.future}\n"
                            if info.future_open_size > 0 or info.spot_open_size > 0:
                                if future_trading_rule and info.future_open_price:
                                    future_open_price = (
                                        info.future_open_price
                                        // future_trading_rule.price_tick
                                        * future_trading_rule.price_tick
                                    )
                                else:
                                    future_open_price = info.future_open_price
                                if spot_trading_rule and info.spot_open_price:
                                    spot_open_price = (
                                        info.spot_open_price
                                        // spot_trading_rule.price_tick
                                        * spot_trading_rule.price_tick
                                    )
                                else:
                                    spot_open_price = info.spot_open_price
                                text += f">Open future: [{future_open_price}, {info.future_open_size}], "
                                text += (
                                    f"spot: [{spot_open_price}, {info.spot_open_size}]"
                                )
                                if future_open_price and spot_open_price:
                                    basis = future_open_price - spot_open_price
                                    text += f", basis: {basis}"
                                text += "\n"
                            if info.future_close_size > 0 or info.spot_close_size > 0:
                                if future_trading_rule and info.future_close_price:
                                    future_close_price = (
                                        info.future_close_price
                                        // future_trading_rule.price_tick
                                        * future_trading_rule.price_tick
                                    )
                                else:
                                    future_close_price = info.future_close_price
                                if spot_trading_rule and info.spot_close_price:
                                    spot_close_price = (
                                        info.spot_close_price
                                        // spot_trading_rule.price_tick
                                        * spot_trading_rule.price_tick
                                    )
                                else:
                                    spot_close_price = info.spot_close_price
                                text += f">Close future: [{future_close_price}, {info.future_close_size}], "
                                text += f"spot: [{spot_close_price}, {info.spot_close_size}]"
                                if future_close_price and spot_close_price:
                                    basis = future_close_price - spot_close_price
                                    text += f", basis: {basis}"
                                text += "\n"

                    self.logger.info(text, slack=self.config.slack_config.enable)

                    # wait next round
                    now = time.time()
                    current_tick = (
                        now // self.LOG_SUMMARY_INTERVAL * self.LOG_SUMMARY_INTERVAL
                    )
                    next_tick = current_tick + self.LOG_SUMMARY_INTERVAL
                    wait_time = next_tick - now
                    await asyncio.sleep(wait_time)
                except Exception:
                    self.logger.error(
                        "Unexcepted error while log summary.",
                        exc_info=True,
                        slack=self.config.slack_config.enable,
                    )
                    await asyncio.sleep(10)
        except asyncio.CancelledError:
            raise

    async def _apply_funding_service(self):
        if not self.config.funding_service_config.enable:
            return
        if self._fs_client is None:
            self._fs_client = FSClient(
                subaccount=self.config.subaccount_name,
                exchange="ftx",
                api_key=self.config.api_key,
            )
        balances: List[dict] = await self.exchange.get_balances()
        usd_info: dict = next(b for b in balances if b["coin"] == "USD")
        usd_borrow: Decimal = Decimal(str(usd_info["spotBorrow"]))
        account_info = await self.exchange.get_account()
        self._update_account_info(account_info)
        current_leverage: Decimal = self.leverage_info.current_leverage
        position: Decimal = self.leverage_info.position_value
        account_value: Decimal = self.leverage_info.account_value
        target_leverage: Decimal = self.config.funding_service_config.target_leverage
        if current_leverage > self.config.funding_service_config.leverage_upper_bound:
            try:
                funding_account_usd_info: dict = (
                    await self._fs_client.get_funding_account_balance_by_coin("USD")
                )
                funding_account_usd_balance: Decimal = Decimal(
                    str(funding_account_usd_info["balance"])
                )
            except Exception:
                self.logger.error(
                    "Unexpected error while request funding account USD balance",
                    exc_info=True,
                    slack=self.config.slack_config.enable,
                )
                return
            net_deposit: Decimal = await self._get_last_24h_net_deposit()
            quote_for_deposit: Decimal = (
                self.config.funding_service_config.daily_max_net_deposit - net_deposit
            )
            if (
                quote_for_deposit
                < self.config.funding_service_config.min_deposit_amount
            ):
                return
            if (
                funding_account_usd_balance
                < self.config.funding_service_config.min_deposit_amount
            ):
                self.logger.warning(
                    f"Funding account USD balance is low -> {funding_account_usd_balance:.2f} USD",
                    slack=self.config.slack_config.enable,
                )
                return
            deposit_amount: Decimal = (position - target_leverage * account_value) / (
                target_leverage + 1
            )
            deposit_amount = min(deposit_amount, funding_account_usd_balance)
            if deposit_amount > self.config.funding_service_config.min_deposit_amount:
                try:
                    resp = await self._fs_client.request_deposit(
                        "USD", round(float(deposit_amount), 2)
                    )
                except (InsufficientBalanceError, Exception) as error:
                    self.logger.error(
                        f"Unexpected error while apply funding service deposit: {error}",
                        exc_info=True,
                        slack=self.config.slack_config.enable,
                    )
                else:
                    await asyncio.sleep(1)
                    status: str = await self._fs_client.get_worker_status(
                        resp["worker_id"]
                    )
                    if status == WORKER_STATUS_FINISH:
                        username = await self.exchange.get_username()
                        balances: List[dict] = await self.exchange.get_balances()
                        usd_info: dict = next(b for b in balances if b["coin"] == "USD")
                        new_usd_borrow: Decimal = Decimal(str(usd_info["spotBorrow"]))
                        account_info = await self.exchange.get_account()
                        self._update_account_info(account_info)
                        new_current_leverage: Decimal = (
                            self.leverage_info.current_leverage
                        )
                        new_position: Decimal = self.leverage_info.position_value
                        new_account_value: Decimal = self.leverage_info.account_value
                        log_msg: str = f"{username} requested deposit ${deposit_amount:.2f} completed\n"
                        log_msg += (
                            f">Account value {account_value} -> {new_account_value}\n"
                        )
                        log_msg += f">Position value {position} -> {new_position}\n"
                        log_msg += (
                            f">USD spot borrow {usd_borrow} -> {new_usd_borrow}\n"
                        )
                        log_msg += f">Leverage {current_leverage}X -> {new_current_leverage}X\n"
                        self.logger.info(log_msg, slack=self.config.slack_config.enable)
                    elif status == WORKER_STATUS_PROC:
                        self.logger.warning(
                            "Funding service deposit is working in progress",
                            slack=self.config.slack_config.enable,
                        )
        elif current_leverage < self.config.funding_service_config.leverage_lower_bound:
            available_usd_for_withdraw: Decimal = Decimal(
                str(usd_info["availableForWithdrawal"])
            )
            available_usd_for_withdraw = max(
                Decimal(0),
                available_usd_for_withdraw
                - self.config.funding_service_config.min_remain,
            )
            withdraw_amount: Decimal = (target_leverage * account_value - position) / (
                target_leverage + 1
            )
            withdraw_amount = min(withdraw_amount, available_usd_for_withdraw)
            if withdraw_amount > self.config.funding_service_config.min_withdraw_amount:
                try:
                    resp = await self._fs_client.request_withdraw(
                        "USD", round(float(withdraw_amount), 2)
                    )
                except (InsufficientBalanceError, Exception) as error:
                    self.logger.error(
                        f"Funding service failed to withdraw ${withdraw_amount}: {error}",
                        exc_info=True,
                        slack=self.config.slack_config.enable,
                    )
                else:
                    await asyncio.sleep(1)
                    status: str = await self._fs_client.get_worker_status(
                        resp["worker_id"]
                    )
                    if status == WORKER_STATUS_FINISH:
                        username = await self.exchange.get_username()
                        balances: List[dict] = await self.exchange.get_balances()
                        usd_info: dict = next(b for b in balances if b["coin"] == "USD")
                        new_usd_borrow: Decimal = Decimal(str(usd_info["spotBorrow"]))
                        account_info = await self.exchange.get_account()
                        self._update_account_info(account_info)
                        new_current_leverage: Decimal = (
                            self.leverage_info.current_leverage
                        )
                        new_position: Decimal = self.leverage_info.position_value
                        new_account_value: Decimal = self.leverage_info.account_value
                        log_msg: str = f"{username} requested withdraw ${withdraw_amount:.2f} completed\n"
                        log_msg += (
                            f">Account value {account_value} -> {new_account_value}\n"
                        )
                        log_msg += f">Position value {position} -> {new_position}\n"
                        log_msg += (
                            f">USD spot borrow {usd_borrow} -> {new_usd_borrow}\n"
                        )
                        log_msg += f">Leverage {current_leverage}X -> {new_current_leverage}X\n"
                        self.logger.info(log_msg, slack=self.config.slack_config.enable)
                    elif status == WORKER_STATUS_PROC:
                        self.logger.warning(
                            "Funding service withdraw is working in progress",
                            slack=self.config.slack_config.enable,
                        )

    async def _apply_funding_service_loop(self):
        await self._account_info_ready_event.wait()
        try:
            while True:
                try:
                    await self._apply_funding_service()
                except Exception:
                    self.logger.error(
                        f"Unexpected error while apply funding service",
                        exc_info=True,
                        slack=self.config.slack_config.enable,
                    )
                finally:
                    await asyncio.sleep(self.FUNDING_SERVICE_INTERVAL)
        except asyncio.CancelledError:
            raise

    def _drop_outdated_deposit_and_withdraw_histroy_cache(self):
        now: float = time.time()
        is_outdated: Callable[[float, dict], bool] = (
            lambda current_ts, history: current_ts
            - dateutil.parser.parse(history["time"]).timestamp()
            > 86400
        )
        self._deposit_history_cache = [
            his for his in self._deposit_history_cache if not is_outdated(now, his)
        ]
        self._withdraw_history_cache = [
            his for his in self._withdraw_history_cache if not is_outdated(now, his)
        ]

    async def _get_last_24h_net_deposit(self) -> Decimal:
        self._drop_outdated_deposit_and_withdraw_histroy_cache()
        end_ts: float = time.time()

        if len(self._deposit_history_cache) == 0:
            start_ts: float = end_ts - 86400
        else:
            start_ts: float = (
                max(
                    [
                        dateutil.parser.parse(his["time"])
                        for his in self._deposit_history_cache
                    ]
                ).timestamp()
                + 1
            )
        deposit_history = await self.exchange.get_deposit_history(start_ts, end_ts)
        self._deposit_history_cache.extend(deposit_history)

        if len(self._withdraw_history_cache) == 0:
            start_ts: float = end_ts - 86400
        else:
            start_ts: float = (
                max(
                    [
                        dateutil.parser.parse(his["time"])
                        for his in self._withdraw_history_cache
                    ]
                ).timestamp()
                + 1
            )
        withdraw_history = await self.exchange.get_withdraw_history(start_ts, end_ts)
        self._withdraw_history_cache.extend(withdraw_history)

        net_deposit: Decimal = Decimal(0)
        for history in self._deposit_history_cache:
            coin: str = history["coin"]
            if coin in ("USD", "USDC", "BUSD"):
                size: Decimal = Decimal(str(history["size"]))
                net_deposit += size
        for history in self._withdraw_history_cache:
            coin: str = history["coin"]
            if coin in ("USD", "USDC", "BUSD"):
                size: Decimal = Decimal(str(history["size"]))
                net_deposit -= size
        return max(Decimal(0), net_deposit)

    async def run(self):
        self.start_network()
        try:
            while True:
                await asyncio.sleep(600)
        except (KeyboardInterrupt, SystemExit):
            self.stop_network()
            await self.exchange.close()
