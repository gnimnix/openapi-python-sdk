# -*- coding: utf-8 -*-
"""
Created on 2018/10/30

@author: gaoan
"""
import json
import logging
import sys
from collections import defaultdict

import stomp
from stomp.exception import ConnectFailedException

from tigeropen import __VERSION__
from tigeropen.common.consts import OrderStatus
from tigeropen.common.consts.params import P_SDK_VERSION, P_SDK_VERSION_PREFIX
from tigeropen.common.consts.push_destinations import QUOTE, QUOTE_DEPTH, QUOTE_FUTURE, QUOTE_OPTION, TRADE_ASSET, \
    TRADE_ORDER, TRADE_POSITION
from tigeropen.common.consts.push_types import RequestType, ResponseType
from tigeropen.common.consts.quote_keys import QuoteChangeKey, QuoteKeyType
from tigeropen.common.util.order_utils import get_order_status
from tigeropen.common.util.signature_utils import sign_with_rsa

HOUR_TRADING_QUOTE_KEYS_MAPPINGS = {'hourTradingLatestPrice': 'latest_price', 'hourTradingPreClose': 'pre_close',
                                    'hourTradingLatestTime': 'latest_time', 'hourTradingVolume': 'volume',
                                    }
QUOTE_KEYS_MAPPINGS = {field.value: field.name for field in QuoteChangeKey}  # like {'askPrice': 'ask_price'}
QUOTE_KEYS_MAPPINGS.update(HOUR_TRADING_QUOTE_KEYS_MAPPINGS)
PRICE_FIELDS = {'open', 'high', 'low', 'close', 'prev_close', 'ask_price', 'bid_price', 'latest_price'}

ASSET_KEYS_MAPPINGS = {'buyingPower': 'buying_power', 'cashBalance': 'cash',
                       'grossPositionValue': 'gross_position_value',
                       'netLiquidation': 'net_liquidation', 'equityWithLoan': 'equity_with_loan',
                       'initMarginReq': 'initial_margin_requirement',
                       'maintMarginReq': 'maintenance_margin_requirement',
                       'availableFunds': 'available_funds', 'excessLiquidity': 'excess_liquidity',
                       'dayTradesRemaining': 'day_trades_remaining', 'currency': 'currency', 'segment': 'segment'}

POSITION_KEYS_MAPPINGS = {'averageCost': 'average_cost', 'position': 'quantity', 'latestPrice': 'market_price',
                          'marketValue': 'market_value', 'orderType': 'order_type', 'realizedPnl': 'realized_pnl',
                          'unrealizedPnl': 'unrealized_pnl', 'secType': 'sec_type', 'localSymbol': 'local_symbol',
                          'originSymbol': 'origin_symbol', 'contractId': 'contract_id', 'symbol': 'symbol',
                          'currency': 'currency', 'strike': 'strike', 'expiry': 'expiry', 'right': 'right',
                          'segment': 'segment', 'identifier': 'identifier'}

ORDER_KEYS_MAPPINGS = {'parentId': 'parent_id', 'orderId': 'order_id', 'orderType': 'order_type',
                       'limitPrice': 'limit_price', 'auxPrice': 'aux_price', 'avgFillPrice': 'avg_fill_price',
                       'totalQuantity': 'quantity', 'filledQuantity': 'filled', 'lastFillPrice': 'last_fill_price',
                       'realizedPnl': 'realized_pnl', 'secType': 'sec_type', 'symbol': 'symbol',
                       'remark': 'reason', 'localSymbol': 'local_symbol', 'originSymbol': 'origin_symbol',
                       'outsideRth': 'outside_rth', 'timeInForce': 'time_in_force', 'openTime': 'order_time',
                       'latestTime': 'trade_time', 'contractId': 'contract_id', 'trailStopPrice': 'trail_stop_price',
                       'trailingPercent': 'trailing_percent', 'percentOffset': 'percent_offset', 'action': 'action',
                       'status': 'status', 'currency': 'currency', 'remaining': 'remaining', 'id': 'id',
                       'segment': 'segment', 'identifier': 'identifier', 'replaceStatus': 'replace_status'}

if sys.platform == 'linux' or sys.platform == 'linux2':
    KEEPALIVE = True
else:
    KEEPALIVE = False


class PushClient(stomp.ConnectionListener):
    def __init__(self, host, port, use_ssl=True, connection_timeout=120, auto_reconnect=True,
                 heartbeats=(30 * 1000, 30 * 1000)):
        """
        :param host:
        :param port:
        :param use_ssl:
        :param connection_timeout: second
        :param auto_reconnect:
        :param heartbeats: tuple of millisecond
        """
        self.host = host
        self.port = port
        self.use_ssl = use_ssl
        self._tiger_id = None
        self._private_key = None
        self._sign = None
        self._stomp_connection = None
        self._destination_counter_map = defaultdict(lambda: 0)

        self.subscribed_symbols = None
        self.quote_changed = None
        self.asset_changed = None
        self.position_changed = None
        self.order_changed = None
        self.connect_callback = None
        self.disconnect_callback = None
        self.subscribe_callback = None
        self.unsubscribe_callback = None
        self.error_callback = None
        self._connection_timeout = connection_timeout
        self._auto_reconnect = auto_reconnect
        self._heartbeats = heartbeats

    def _connect(self):
        try:
            if self._stomp_connection:
                self._stomp_connection.remove_listener('push')
                self._stomp_connection.transport.cleanup()
        except:
            pass

        self._stomp_connection = stomp.Connection12(host_and_ports=[(self.host, self.port), ], use_ssl=self.use_ssl,
                                                    keepalive=KEEPALIVE, timeout=self._connection_timeout,
                                                    heartbeats=self._heartbeats)
        self._stomp_connection.set_listener('push', self)
        self._stomp_connection.start()
        try:
            self._stomp_connection.connect(self._tiger_id, self._sign, wait=True, headers=self._generate_headers())
        except ConnectFailedException as e:
            raise e

    def connect(self, tiger_id, private_key):
        self._tiger_id = tiger_id
        self._private_key = private_key
        self._sign = sign_with_rsa(self._private_key, self._tiger_id, 'utf-8')
        self._connect()

    def disconnect(self):
        if self._stomp_connection:
            self._stomp_connection.disconnect()

    def on_connected(self, headers, body):
        if self.connect_callback:
            self.connect_callback()

    def on_disconnected(self):
        if self.disconnect_callback:
            self.disconnect_callback()
        elif self._auto_reconnect:
            self._connect()

    def on_message(self, headers, body):
        """
        Called by the STOMP connection when a MESSAGE frame is received.

        :param dict headers: a dictionary containing all headers sent by the server as key/value pairs.
        :param body: the frame's payload - the message body.
        """
        try:
            response_type = headers.get('ret-type')
            if response_type == str(ResponseType.GET_SUB_SYMBOLS_END.value):
                if self.subscribed_symbols:
                    data = json.loads(body)
                    limit = data.get('limit')
                    symbols = data.get('subscribedSymbols')
                    used = data.get('used')
                    symbol_focus_keys = data.get('symbolFocusKeys')
                    focus_keys = dict()
                    for sym, keys in symbol_focus_keys.items():
                        keys = set(QUOTE_KEYS_MAPPINGS.get(key, key) for key in keys)
                        focus_keys[sym] = list(keys)
                    self.subscribed_symbols(symbols, focus_keys, limit, used)
            elif response_type == str(ResponseType.GET_QUOTE_CHANGE_END.value):
                if self.quote_changed:
                    data = json.loads(body)
                    hour_trading = False
                    if 'hourTradingLatestPrice' in data:
                        hour_trading = True
                    if 'symbol' in data:
                        symbol = data.get('symbol')
                        offset = data.get('offset', 0)
                        items = []
                        # 期货行情推送的价格都乘了 10 的 offset 次方变成了整数, 需要除回去变为正常单位的价格
                        if offset:
                            for key, value in data.items():
                                if key == 'latestTime' or key == 'hourTradingLatestTime':
                                    continue
                                if key in QUOTE_KEYS_MAPPINGS:
                                    key = QUOTE_KEYS_MAPPINGS.get(key)
                                    if key in PRICE_FIELDS:
                                        value /= 10 ** offset
                                    elif key == 'minute':
                                        minute_item = dict()
                                        for m_key, m_value in value.items():
                                            if m_key in {'p', 'h', 'l'}:
                                                m_value /= 10 ** offset
                                            minute_item[m_key] = m_value
                                            value = minute_item
                                    items.append((key, value))
                        else:
                            for key, value in data.items():
                                if key == 'latestTime' or key == 'hourTradingLatestTime':
                                    continue
                                if key in QUOTE_KEYS_MAPPINGS:
                                    key = QUOTE_KEYS_MAPPINGS.get(key)
                                    items.append((key, value))
                        if items:
                            self.quote_changed(symbol, items, hour_trading)
            elif response_type == str(ResponseType.SUBSCRIBE_ASSET.value):
                if self.asset_changed:
                    data = json.loads(body)
                    if 'account' in data:
                        account = data.get('account')
                        items = []
                        for key, value in data.items():
                            if key in ASSET_KEYS_MAPPINGS:
                                items.append((ASSET_KEYS_MAPPINGS.get(key), value))
                        if items:
                            self.asset_changed(account, items)
            elif response_type == str(ResponseType.SUBSCRIBE_POSITION.value):
                if self.position_changed:
                    data = json.loads(body)
                    if 'account' in data:
                        account = data.get('account')
                        items = []
                        for key, value in data.items():
                            if key in POSITION_KEYS_MAPPINGS:
                                items.append((POSITION_KEYS_MAPPINGS.get(key), value))
                        if items:
                            self.position_changed(account, items)
            elif response_type == str(ResponseType.SUBSCRIBE_ORDER_STATUS.value):
                if self.order_changed:
                    data = json.loads(body)
                    if 'account' in data:
                        account = data.get('account')
                        items = []
                        for key, value in data.items():
                            if key in ORDER_KEYS_MAPPINGS:
                                if key == 'status':
                                    value = get_order_status(value)
                                    # 部分成交 (服务端推送 'Submitted' 状态)
                                    if value == OrderStatus.HELD and data.get('filledQuantity'):
                                        value = OrderStatus.PARTIALLY_FILLED
                                items.append((ORDER_KEYS_MAPPINGS.get(key), value))
                        if items:
                            self.order_changed(account, items)
            elif response_type == str(ResponseType.GET_SUBSCRIBE_END.value):
                if self.subscribe_callback:
                    self.subscribe_callback(headers.get('destination'), json.loads(body))
            elif response_type == str(ResponseType.GET_CANCEL_SUBSCRIBE_END.value):
                if self.unsubscribe_callback:
                    self.unsubscribe_callback(headers.get('destination'), json.loads(body))
            elif response_type == str(ResponseType.ERROR_END.value):
                if self.error_callback:
                    self.error_callback(body)
        except Exception as e:
            logging.error(e, exc_info=True)

    def on_error(self, headers, body):
        if self.error_callback:
            self.error_callback(body)
        else:
            logging.error(body)

    def _update_subscribe_id(self, destination):
        self._destination_counter_map[destination] += 1

    def _get_subscribe_id(self, destination):
        return 'sub-' + str(self._destination_counter_map[destination])

    def subscribe_asset(self, account=None):
        """
        订阅账户资产更新
        :return:
        """
        return self._handle_trade_subscribe(TRADE_ASSET, 'Asset', account)

    def unsubscribe_asset(self, id=None):
        """
        退订账户资产更新
        :return:
        """
        self._handle_trade_unsubscribe(TRADE_ASSET, 'Asset', sub_id=id)

    def subscribe_position(self, account=None):
        """
        订阅账户持仓更新
        :return:
        """
        return self._handle_trade_subscribe(TRADE_POSITION, 'Position', account)

    def unsubscribe_position(self, id=None):
        """
        退订账户持仓更新
        :return:
        """
        self._handle_trade_unsubscribe(TRADE_POSITION, 'Position', sub_id=id)

    def subscribe_order(self, account=None):
        """
        订阅账户订单更新
        :return:
        """
        return self._handle_trade_subscribe(TRADE_ORDER, 'OrderStatus', account)

    def unsubscribe_order(self, id=None):
        """
        退订账户订单更新
        :return:
        """
        self._handle_trade_unsubscribe(TRADE_ORDER, 'OrderStatus', sub_id=id)

    def subscribe_quote(self, symbols, quote_key_type=QuoteKeyType.TRADE, focus_keys=None):
        """
        订阅行情更新
        :param symbols:
        :param quote_key_type: 行情类型, 值为 common.consts.quote_keys.QuoteKeyType 枚举类型
        :param focus_keys: 行情 key
        :return:
        """
        extra_headers = dict()
        if focus_keys:
            keys = list()
            for key in focus_keys:
                if isinstance(key, str):
                    keys.append(key)
                else:
                    keys.append(key.value)
            extra_headers['keys'] = ','.join(keys)
        elif quote_key_type and quote_key_type.value:
            extra_headers['keys'] = quote_key_type.value
        return self._handle_quote_subscribe(destination=QUOTE, subscription='Quote', symbols=symbols,
                                            extra_headers=extra_headers)

    def subscribe_depth_quote(self, symbols):
        """
        订阅深度行情
        :param symbols: symbol列表
        :return:
        """
        return self._handle_quote_subscribe(destination=QUOTE_DEPTH, subscription='QuoteDepth', symbols=symbols)

    def subscribe_option(self, symbols):
        """
        订阅期权行情
        :param symbols: symbol列表
        :return:
        """
        return self._handle_quote_subscribe(destination=QUOTE_OPTION, subscription='Option', symbols=symbols)

    def subscribe_future(self, symbols):
        """
        订阅期货行情
        :param symbols: symbol列表
        :return:
        """
        return self._handle_quote_subscribe(destination=QUOTE_FUTURE, subscription='Future', symbols=symbols)

    def query_subscribed_quote(self):
        """
        查询已订阅行情的合约
        :return:
        """
        headers = self._generate_headers()
        headers['destination'] = QUOTE
        headers['req-type'] = RequestType.REQ_SUB_SYMBOLS.value
        self._stomp_connection.send(QUOTE, "{}", headers=headers)

    def unsubscribe_quote(self, symbols=None, id=None):
        """
        退订行情更新
        :return:
        """
        self._handle_quote_unsubscribe(destination=QUOTE, subscription='Quote', sub_id=id, symbols=symbols)

    def unsubscribe_depth_quote(self, symbols=None, id=None):
        """
        退订深度行情更新
        :return:
        """
        self._handle_quote_unsubscribe(destination=QUOTE_DEPTH, subscription='QuoteDepth', sub_id=id, symbols=symbols)

    def _handle_trade_subscribe(self, destination, subscription, account=None, extra_headers=None):
        if extra_headers is None:
            extra_headers = dict()
        if account is not None:
            extra_headers['account'] = account
        return self._handle_subscribe(destination=destination, subscription=subscription, extra_headers=extra_headers)

    def _handle_quote_subscribe(self, destination, subscription, symbols=None, extra_headers=None):
        if extra_headers is None:
            extra_headers = dict()
        if symbols is not None:
            extra_headers['symbols'] = ','.join(symbols)
        return self._handle_subscribe(destination=destination, subscription=subscription, extra_headers=extra_headers)

    def _handle_trade_unsubscribe(self, destination, subscription, sub_id=None):
        self._handle_unsubscribe(destination=destination, subscription=subscription, sub_id=sub_id)

    def _handle_quote_unsubscribe(self, destination, subscription, sub_id=None, symbols=None):
        extra_headers = dict()
        if symbols is not None:
            extra_headers['symbols'] = ','.join(symbols)
        self._handle_unsubscribe(destination=destination, subscription=subscription, sub_id=sub_id,
                                 extra_headers=extra_headers)

    def _handle_subscribe(self, destination, subscription, extra_headers=None):
        headers = self._generate_headers(extra_headers)
        headers['destination'] = destination
        headers['subscription'] = subscription
        self._update_subscribe_id(destination)
        sub_id = self._get_subscribe_id(destination)
        headers['id'] = sub_id

        self._stomp_connection.subscribe(destination, id=sub_id, headers=headers)
        return sub_id

    def _handle_unsubscribe(self, destination, subscription, sub_id=None, extra_headers=None):
        headers = self._generate_headers(extra_headers)
        headers['destination'] = destination
        headers['subscription'] = subscription
        id_ = sub_id if sub_id is not None else self._get_subscribe_id(destination)
        headers['id'] = id_

        self._stomp_connection.unsubscribe(id=id_, headers=headers)

    def _generate_headers(self, extra_headers=None):
        headers = {P_SDK_VERSION: P_SDK_VERSION_PREFIX + __VERSION__}
        if extra_headers is not None:
            headers.update(extra_headers)
        return headers

