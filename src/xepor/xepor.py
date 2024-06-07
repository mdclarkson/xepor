import functools
import logging
import os
import re
import sys
import traceback
import urllib.parse
from enum import Enum
from typing import List, Optional, Tuple, Union

from mitmproxy.connection import Server
from mitmproxy.http import HTTPFlow, Response
from mitmproxy.net.http import url
from parse import Parser

__author__ = "ttimasdf"
__copyright__ = "ttimasdf"
__license__ = "Apache-2.0"


class RouteType(Enum):
    """
    This enum is an option set in route definition, specify it to be matched
    on either incoming request or response.
    """

    REQUEST = 1
    """The route will be matched on mitmproxy ``request`` event"""
    RESPONSE = 2
    """The route will be matched on mitmproxy ``response`` event"""


class FlowMeta(Enum):
    """
    This class is used internally by Xepor to mark ``flow`` object by certain metadata.
    Refer to the source code for detailed usage.
    """

    REQ_PASSTHROUGH = "xepor-request-passthrough"
    RESP_PASSTHROUGH = "xepor-response-passthrough"
    REQ_URLPARSE = "xepor-request-urlparse"
    REQ_HOST = "xepor-request-host"


class InterceptedAPI:
    """
    the InterceptedAPI object is the central registry of your view functions.
    Users should use a function decorator :func:`route` to define and register
    URL and host mapping to the view functions. Just like flask's :external:py:meth:`flask.Flask.route`.

    .. code-block:: python

        from xepor import InterceptedAPI, RouteType

        HOST_HTTPBIN = "httpbin.org"
        api = InterceptedAPI(HOST_HTTPBIN)

    Defining a constant for your target (victim) domain name is not mandatory
    (even the `default_host` parameter itself is optional) but
    recommanded as a best practise. If you have multiple hosts to inject
    (see an example at `xepor/xepor-examples/polyv_scrapper/polyv.py <https://github.com/xepor/xepor-examples/blob/306ffad36a9ff3db00eb44b67b8b83a85e234d6e/polyv_scrapper/polyv.py#L27-L29>`_), you would have to specify the domain name
    multiple times in each :func:`route` in `host` parameter,
    (especially for domains other than `default_host`).
    So it's better to have a variable for that.

    Add route via function call similar to Flask :external:py:meth:`flask.Flask.add_url_rule`
    is not yet implemented.

    :param default_host: The default host to forward requests to.
    :param host_mapping: A list of tuples of the form (regex, host) where regex
        is a regular expression to match against the request host and host is the
        host to redirect the request to.
    :param blacklist_domain: A list of domains to not forward requests to.
        The requests and response from hosts in this list will not respect
        `request_passthrough` and `response_passthrough` setting.
    :param request_passthrough: Whether to forward the request to upstream server
        if no route is found. If `request_passthrough = False`, all requests not
        matching any route will be responded with :func:`default_response` without
        connecting to upstream.
    :param response_passthrough: Whether to forward the response to the user
        if no route is found. If `response_passthrough = False`, all responses not
        matching any route will be replaced with the Response object
        generated by :func:`default_response`.
    :param respect_proxy_headers: Set to `True` only when you use Xepor as
        a web server behind a reverse proxy. Typical use case is to set up an
        mitmproxy in ``reverse`` mode to bypass some online license checks.
        Xepor will respect the following headers and strip them from requests to upstream.

        - `X-Forwarded-For`
        - `X-Forwarded-Host`
        - `X-Forwarded-Port`
        - `X-Forwarded-Proto`
        - `X-Forwarded-Server`
        - `X-Real-Ip`
    """

    _REGEX_HOST_HEADER = re.compile(r"^(?P<host>[^:]+|\[.+\])(?::(?P<port>\d+))?$")

    _PROXY_FORWARDED_HEADERS = [
        "X-Forwarded-For",
        "X-Forwarded-Host",
        "X-Forwarded-Port",
        "X-Forwarded-Proto",
        "X-Forwarded-Server",
        "X-Real-Ip",
    ]

    def __init__(
        self,
        default_host: Optional[str] = None,
        host_mapping: Optional[List[Tuple[Union[str, re.Pattern], str]]] = None,
        blacklist_domain: Optional[List[str]] = None,
        request_passthrough: bool = True,
        response_passthrough: bool = True,
        respect_proxy_headers: bool = False,
    ):
        host_mapping = {} if host_mapping is None else host_mapping
        blacklist_domain = [] if blacklist_domain is None else blacklist_domain

        self.default_host = default_host
        self.host_mapping = host_mapping
        self.request_routes: List[Tuple[Optional[str], Parser, callable]] = []
        self.response_routes: List[Tuple[Optional[str], Parser, callable]] = []
        self.blacklist_domain = blacklist_domain
        self.request_passthrough = request_passthrough
        self.response_passthrough = response_passthrough
        self.respect_proxy_headers = respect_proxy_headers

        self._log = logging.getLogger(__name__)
        if os.getenv("XEPOR_LOG_DEBUG"):
            self._log.setLevel(logging.DEBUG)
        self._log.info("%s started", self.__class__.__name__)

    # def server_connect(self, data: ServerConnectionHookData):
    #     self._log.debug("Getting connection: peer=%s sock=%s addr=%s . state=%s",
    #         data.server.peername, data.server.sockname, data.server.address, data.server)

    def request(self, flow: HTTPFlow):
        """
        This function is called by the mitmproxy framework whenever a request is made.

        :param flow: The :class:`~mitmproxy.http.HTTPFlow` object from client request.
        :return: None
        """
        if FlowMeta.REQ_URLPARSE in flow.metadata:
            url = flow.metadata[FlowMeta.REQ_URLPARSE]
        else:
            url = urllib.parse.urlparse(flow.request.path)
            flow.metadata[FlowMeta.REQ_URLPARSE] = url
        path = url.path
        if flow.metadata.get(FlowMeta.REQ_PASSTHROUGH) is True:
            self._log.warning(
                "<= [%s] %s skipped because of previous passthrough",
                flow.request.method,
                path,
            )
            return
        host = self.remap_host(flow)
        handler, params = self.find_handler(host, path, RouteType.REQUEST)
        if handler is not None:
            self._log.info("<= [%s] %s", flow.request.method, path)
            handler(flow, *params.fixed, **params.named)
        elif (
            not self.request_passthrough
            or self.get_host(flow)[0] in self.blacklist_domain
        ):
            self._log.warning("<= [%s] %s default response", flow.request.method, path)
            flow.response = self.default_response()
        else:
            flow.metadata[FlowMeta.REQ_PASSTHROUGH] = True
            self._log.debug("<= [%s] %s passthrough", flow.request.method, path)

    def response(self, flow: HTTPFlow):
        """
        This function is called by the mitmproxy when a response is returned the server.

        :param flow: The :class:`~mitmproxy.http.HTTPFlow` object from server response.
        :return: None
        """
        if FlowMeta.REQ_URLPARSE in flow.metadata:
            url = flow.metadata[FlowMeta.REQ_URLPARSE]
        else:
            url = urllib.parse.urlparse(flow.request.path)
            flow.metadata[FlowMeta.REQ_URLPARSE] = url
        path = url.path
        if flow.metadata.get(FlowMeta.RESP_PASSTHROUGH) is True:
            self._log.warning(
                "=> [%s] %s skipped because of previous passthrough",
                flow.response.status_code,
                path,
            )
            return
        handler, params = self.find_handler(
            self.get_host(flow)[0], path, RouteType.RESPONSE
        )
        if handler is not None:
            self._log.info("=> [%s] %s", flow.response.status_code, path)
            handler(flow, *params.fixed, **params.named)
        elif (
            not self.response_passthrough
            or self.get_host(flow)[0] in self.blacklist_domain
        ):
            self._log.warning(
                "=> [%s] %s default response", flow.response.status_code, path
            )
            flow.response = self.default_response()
        else:
            flow.metadata[FlowMeta.RESP_PASSTHROUGH] = True
            self._log.debug("=> [%s] %s passthrough", flow.response.status_code, path)

    def route(
        self: str,
        path: str,
        host: Optional[str] = None,
        rtype: RouteType = RouteType.REQUEST,
        catch_error: bool = True,
        return_error: bool = False,
    ):
        """
        This is the main API used by end users.
        It decorate a view function to register it with given host and URL.

        Typical usage (taken from official example: `httpbin.py <https://github.com/xepor/xepor-examples/blob/main/httpbin/httpbin.py>`_):

        .. code-block:: python

            @api.route("/get")
            def change_your_request(flow: HTTPFlow):
                flow.request.query["payload"] = "evil_param"

            @api.route("/basic-auth/{usr}/{pwd}", rtype=RouteType.RESPONSE)
            def capture_auth(flow: HTTPFlow, usr=None, pwd=None):
                print(
                    f"auth @ {usr} + {pwd}:",
                    f"Captured {'successful' if flow.response.status_code < 300 else 'unsuccessful'} login:",
                    flow.request.headers.get("Authorization", ""),
                )

        See Github: `xepor/xepor-examples <https://github.com/xepor/xepor-examples>`_ for more examples.


        :param path: The URL path to be routed.
            The path definition grammar is similar to Python 3 :func:`~str.format`.
            Check the documentation of ``parse`` library:
            `r1chardj0n3s/parse <https://github.com/r1chardj0n3s/parse>`_

        :param host: The host to be routed.
            This value will be matched against the following fields of
            incoming flow object by order:

            1. ``X-Forwarded-For`` Header. (only when `respect_proxy_headers` in :class:`InterceptedAPI` is `True`)
            2. HTTP ``Host`` Header, if exists.
            3.  ``flow.host`` reported by underlying layer.
                In HTTP or Socks5h proxy mode, it may hopefully be a hostname,
                otherwise, it'll be an IP address.

        :param rtype: Set the route be matched on either request or response.
            Accepting :class:`RouteType`.

        :param catch_error: If set to `True`, the exception inside the route
            will be handled by Xepor.

            If set to `False`, the exception will be raised and handled by mitmproxy.

        :param return_error: If set to `True`, the error message inside the exception
            (``str(exc)``) will be returned to client. This behaviour can be overrided
            through :func:`error_response`.

            If set to `False`, the exception will be printed to console,
            the ``flow`` object will be passed to mitmproxy continuely.

            .. admonition:: Note

                When exception occured, the ``flow`` object do `not` always stay intact.
                This option is only a try-catch like normal Python code. If you run
                ``modify1(flow) and modify2(flow) and modify3(flow)`` and exception raised
                in ``modify2()``, the ``flow`` object will be modified partially.

        :return: The decorated function.
        """
        host = host or self.default_host

        def catcher(func):
            """
            The internal wrapper for catching exceptions
            if `catch_error` is specified.
            """

            @functools.wraps(func)
            def handler(flow: HTTPFlow, *args, **kwargs):
                try:
                    return func(flow, *args, **kwargs)
                except Exception as e:
                    etype, value, tback = sys.exc_info()
                    tb = "".join(traceback.format_exception(etype, value, tback))
                    self._log.error(
                        "Exception catched when handling response to %s:\n%s",
                        flow.request.pretty_url,
                        tb,
                    )
                    if return_error:
                        flow.response = self.error_response(str(e))

            return handler

        def wrapper(handler):
            if catch_error:
                handler = catcher(handler)
            if rtype == RouteType.REQUEST:
                self.request_routes.append((host, Parser(path), handler))
            elif rtype == RouteType.RESPONSE:
                self.response_routes.append((host, Parser(path), handler))
            else:
                raise ValueError(f"Invalid route type: {rtype}")
            return handler

        return wrapper

    def remap_host(self, flow: HTTPFlow, overwrite=True):
        """
        Remaps the host of the flow to the destination host.

        .. admonition:: Note

            This function is used internally by Xepor.
            Refer to the source code for customization.

        :param flow: The flow to remap.
        :param overwrite: Whether to overwrite the host and port of the flow.
        :return: The remapped host.
        """
        host, port = self.get_host(flow)
        for src, dest in self.host_mapping:
            if (isinstance(src, re.Pattern) and src.match(host)) or (
                isinstance(src, str) and host == src
            ):
                if overwrite and (
                    flow.request.host != dest or flow.request.port != port
                ):
                    if self.respect_proxy_headers:
                        flow.request.scheme = flow.request.headers["X-Forwarded-Proto"]
                    flow.server_conn = Server((dest, port))
                    flow.request.host = dest
                    flow.request.port = port
                self._log.debug(
                    "flow: %s, remapping host: %s -> %s, port: %d",
                    flow,
                    host,
                    dest,
                    port,
                )
                return dest
        return host

    def get_host(self, flow: HTTPFlow) -> Tuple[str, int]:
        """
        Gets the host and port of the request.
        Extending from mitmproxy's ``flow.pretty_host`` to accept
        values from proxy headers(``X-Forwarded-Host`` and ``X-Forwarded-Port``)

        .. admonition:: Note

            This function is used internally by Xepor.
            Refer to the source code for customization.

        :param flow: The HTTPFlow object.
        :return: A tuple of the host and port.
        """
        if FlowMeta.REQ_HOST not in flow.metadata:
            if self.respect_proxy_headers:
                # all(h in flow.request.headers for h in ["X-Forwarded-Host", "X-Forwarded-Port"])
                host = flow.request.headers["X-Forwarded-Host"]
                port = int(flow.request.headers["X-Forwarded-Port"])
            else:
                # Get Destination Host
                host, port = url.parse_authority(flow.request.pretty_host, check=False)
                port = port or url.default_port(flow.request.scheme) or 80
            flow.metadata[FlowMeta.REQ_HOST] = (host, port)
        return flow.metadata[FlowMeta.REQ_HOST]

    def default_response(self):
        """
        This is the default response function for Xepor.
        It will be called in following conditions:

        1. target host in HTTP request matches the ones in `blacklist_domain`.
        2. either `request_passthrough` or `response_passthrough` is set to `False`,
           and no route matches the incoming flow.

        Override this function if it suits your needs.

        :return: A Response object with status code 404
            and HTTP header ``X-Intercepted-By`` set to ``xepor``.
        """
        return Response.make(404, "Not Found", {"X-Intercepted-By": "xepor"})

    def error_response(self, msg: str = "APIServer Error"):
        """
        Returns a response with status code 502 and custom error message.

        Override this function if it suits your needs.

        :param msg: The message to be returned.

        :return: A Response object with status code 502
            and content set to the .
        """
        return Response.make(502, msg)

    def find_handler(self, host, path, rtype=RouteType.REQUEST):
        """
        Finds the appropriate handler for the request.

        .. admonition:: Note

            This function is used internally by Xepor.
            Refer to the source code for customization.

        :param host: The host of the request.
        :param path: The path of the request.
        :param rtype: The type of the route. Accepting :class:`RouteType`.
        :return: The handler and the parse result.
        """
        if rtype == RouteType.REQUEST:
            routes = self.request_routes
        elif rtype == RouteType.RESPONSE:
            routes = self.response_routes
        else:
            raise ValueError(f"Invalid route type: {rtype}")

        for h, parser, handler in routes:
            if h != host:
                continue
            parse_result = parser.parse(path)
            self._log.debug("Parse %s => %s", path, parse_result)
            if parse_result is not None:
                return handler, parse_result

        return None, None
