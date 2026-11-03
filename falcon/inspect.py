# Copyright 2020-2026 by Federico Caselli
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Inspect utilities for falcon applications."""

from __future__ import annotations

import ast
from collections import deque
from collections.abc import Iterable
from collections.abc import Iterator
from functools import partial
import inspect
import re
from typing import Any, Callable, cast, Optional, Union

from falcon import app_helpers
from falcon._typing import _ReqT
from falcon._typing import _RespT
from falcon.app import App
from falcon.routing import CompiledRouter
from falcon.routing import converters
from falcon.routing.compiled import _FIELD_PATTERN
from falcon.routing.compiled import CompiledRouterNode


def inspect_app(app: App[_ReqT, _RespT]) -> AppInfo:
    """Inspects an application.

    Args:
        app (falcon.App): The application to inspect. Works with both
            :class:`falcon.App` and :class:`falcon.asgi.App`.

    Returns:
        AppInfo: The information regarding the application. Call
        :meth:`~.AppInfo.to_string` on the result to obtain a human-friendly
        representation.
    """
    routes = inspect_routes(app)
    static = inspect_static_routes(app)
    sinks = inspect_sinks(app)
    error_handlers = inspect_error_handlers(app)
    middleware = inspect_middleware(app)
    return AppInfo(routes, middleware, static, sinks, error_handlers, app._ASGI)


def inspect_routes(app: App[_ReqT, _RespT]) -> list[RouteInfo]:
    """Inspects the routes of an application.

    Args:
        app (falcon.App): The application to inspect. Works with both
            :class:`falcon.App` and :class:`falcon.asgi.App`.

    Returns:
        list[RouteInfo]: A list of route descriptions for the application.
    """
    router = app._router

    inspect_function = _supported_routers.get(type(router))
    if inspect_function is None:
        raise TypeError(
            'Unsupported router class {}. Use "register_router" '
            'to register a function that can inspect the router '
            'used by the provided application'.format(type(router))
        )
    return cast(list[RouteInfo], inspect_function(router))


def register_router(
    router_class: type,
) -> Callable[..., Callable[..., list[RouteInfo]]]:
    """Register a function to inspect a particular router.

    This decorator registers a new function for a custom router
    class, so that it can be inspected with the function
    :func:`.inspect_routes`.
    An inspection function takes the router instance used by the
    application and returns a list of :class:`.RouteInfo`. Eg::

        @register_router(MyRouterClass)
        def inspect_my_router(router):
            return [RouteInfo('foo', 'bar', '/path/to/foo.py:42', [])]

    Args:
        router_class (Type): The router class to register. If
            already registered an error will be raised.
    """

    def wraps(fn: Callable[..., list[RouteInfo]]) -> Callable[..., list[RouteInfo]]:
        if router_class in _supported_routers:
            raise ValueError(
                'Another function is already registered for the router {}'.format(
                    router_class
                )
            )
        _supported_routers[router_class] = fn
        return fn

    return wraps


_supported_routers: dict[type, Callable[..., Any]] = {}


def inspect_static_routes(app: App[_ReqT, _RespT]) -> list[StaticRouteInfo]:
    """Inspects the static routes of an application.

    Args:
        app (falcon.App): The application to inspect. Works with both
            :class:`falcon.App` and :class:`falcon.asgi.App`.

    Returns:
        list[StaticRouteInfo]: A list of static routes that have
        been added to the application.
    """
    routes = []
    for sr, _, _ in app._static_routes:
        info = StaticRouteInfo(sr._prefix, sr._directory, sr._fallback_filename)
        routes.append(info)
    return routes


def inspect_sinks(app: App[_ReqT, _RespT]) -> list[SinkInfo]:
    """Inspects the sinks of an application.

    Args:
        app (falcon.App): The application to inspect. Works with both
            :class:`falcon.App` and :class:`falcon.asgi.App`.

    Returns:
        list[SinkInfo]: A list of sinks used by the application.
    """
    sinks = []
    for prefix, sink, _ in app._sinks:
        source_info, name = _get_source_info_and_name(sink)
        assert source_info
        info = SinkInfo(prefix.pattern, name, source_info)
        sinks.append(info)
    return sinks


def inspect_error_handlers(app: App[_ReqT, _RespT]) -> list[ErrorHandlerInfo]:
    """Inspects the error handlers of an application.

    Args:
        app (falcon.App): The application to inspect. Works with both
            :class:`falcon.App` and :class:`falcon.asgi.App`.

    Returns:
        list[ErrorHandlerInfo]: A list of error handlers used by the
        application.
    """
    errors = []
    for exc, fn in app._error_handlers.items():
        source_info, name = _get_source_info_and_name(fn)
        assert source_info
        info = ErrorHandlerInfo(exc.__name__, name, source_info, _is_internal(fn))
        errors.append(info)
    return errors


def inspect_middleware(app: App[_ReqT, _RespT]) -> MiddlewareInfo:
    """Inspects the middleware components of an application.

    Args:
        app (falcon.App): The application to inspect. Works with both
            :class:`falcon.App` and :class:`falcon.asgi.App`.

    Returns:
        MiddlewareInfo: Information about the app's middleware components.
    """
    types_ = app_helpers.prepare_middleware(app._unprepared_middleware, True, app._ASGI)

    type_infos = []
    for stack in types_:
        current = []
        for method in stack:
            _, name = _get_source_info_and_name(method)
            cls = type(method.__self__)  # type: ignore[union-attr]
            _, cls_name = _get_source_info_and_name(cls)
            current.append(MiddlewareTreeItemInfo(name, cls_name))
        type_infos.append(current)
    middlewareTree = MiddlewareTreeInfo(*type_infos)

    middlewareClasses = []
    names = 'Process request', 'Process resource', 'Process response'
    for m in app._unprepared_middleware:
        fns = app_helpers.prepare_middleware([m], True, app._ASGI)
        class_source_info, cls_name = _get_source_info_and_name(type(m))
        methods = []
        for method, name in zip(fns, names):  # type: ignore[assignment]
            if method:
                real_func = method[0]  # type: ignore[index]
                source_info = _get_source_info(real_func)
                assert source_info
                methods.append(MiddlewareMethodInfo(real_func.__name__, source_info))  # type: ignore[union-attr]
        assert class_source_info
        m_info = MiddlewareClassInfo(cls_name, class_source_info, methods)
        middlewareClasses.append(m_info)

    return MiddlewareInfo(
        middlewareTree, middlewareClasses, app._independent_middleware
    )


@register_router(CompiledRouter)
def inspect_compiled_router(router: CompiledRouter) -> list[RouteInfo]:
    """Walk an instance of :class:`~.CompiledRouter` to return a list of defined routes.

    Default route inspector for CompiledRouter.

    Args:
        router (CompiledRouter): The router to inspect.

    Returns:
        list[RouteInfo]: A list of :class:`~.RouteInfo`.
    """

    def _traverse(roots: list[CompiledRouterNode], parent: str) -> None:
        for root in roots:
            path = parent + '/' + root.raw_segment
            if root.resource is not None:
                methods = []
                if root.method_map:
                    for method, func in root.method_map.items():
                        if isinstance(func, partial):
                            real_func = func.func
                        else:
                            real_func = func

                        source_info = _get_source_info(real_func)
                        internal = _is_internal(real_func)
                        assert source_info, (
                            'This is for type checking only, as here source '
                            'info will always be a string'
                        )
                        method_info = RouteMethodInfo(
                            method, source_info, real_func.__name__, internal
                        )
                        methods.append(method_info)
                source_info, class_name = _get_source_info_and_name(root.resource)
                assert source_info
                route_info = RouteInfo(path, class_name, source_info, methods)
                routes.append(route_info)

            if root.children:
                _traverse(root.children, path)

    routes: list[RouteInfo] = []
    _traverse(router._roots, '')
    return routes


def inspect_route_reachability(
    app: App[_ReqT, _RespT],
) -> list[RouteReachabilityInfo]:
    """Statically determine whether each route may ever be reached.

    The default router evaluates sibling templates in a fixed order
    (static segments first, then complex field segments in registration
    order, and finally a simple field segment). Consequently, some route
    templates are always shadowed by routes registered earlier. This
    function analyzes the router tree without compiling it, matching a
    request, or invoking any user-provided converter or responder code.

    Only a conservative subset of templates can be analyzed exactly:

    *   Literal parts may only contain ASCII letters and digits as well
        as the characters ``.``, ``_``, ``-`` (and ``/`` as the path
        separator).
    *   Fields must be simple fields (e.g. ``{name}``), built-in ``int``
        converter fields whose ``num_digits``, ``min``, and ``max``
        arguments are integer or ``None`` literals (e.g.
        ``{id:int(min=1, max=99)}``), or a built-in ``path`` converter
        field at the end of the template (e.g. ``{rest:path}``).

    For every analyzed route, the result is either a concrete example
    path that reaches it, the reason why it is unreachable, or an
    :attr:`~.RouteReachabilityInfo.UNKNOWN` status when the template (or
    one that may intercept it first) is outside the supported subset.

    The analysis is read-only and does not influence request dispatch.

    Args:
        app (falcon.App): The application to inspect. Works with both
            :class:`falcon.App` and :class:`falcon.asgi.App`.

    Returns:
        list[RouteReachabilityInfo]: Reachability information for each
        route, in router tree order.

    Raises:
        TypeError: If the application uses a custom router class that
            does not subclass :class:`.CompiledRouter`.
    """
    router = app._router
    if not isinstance(router, CompiledRouter):
        raise TypeError(
            'Route reachability inspection is only supported by the '
            'default CompiledRouter, but the application uses {}.'.format(type(router))
        )

    results: list[RouteReachabilityInfo] = []
    _reachability_walk(router, router._roots, [], None, results)
    return results


# ------------------------------------------------------------------------
# Inspection classes
# ------------------------------------------------------------------------


class _Traversable:
    __visit_name__ = 'N/A'

    def to_string(self, verbose: bool = False, internal: bool = False) -> str:
        """Return a string representation of this class.

        Args:
            verbose (bool, optional): Adds more information. Defaults to False.
            internal (bool, optional): Also include internal route methods
                and error handlers added by the framework. Defaults to
                ``False``.

        Returns:
            str: string representation of this class.
        """
        return StringVisitor(verbose, internal).process(self)

    def __repr__(self) -> str:
        return self.to_string()


class RouteMethodInfo(_Traversable):
    """Describes a responder method.

    Args:
        method (str): The HTTP method of this responder.
        source_info (str): The source path of this function.
        function_name (str): Name of the function.
        internal (bool): Whether or not this was a default responder added
            by the framework.

    """

    __visit_name__ = 'route_method'

    suffix: str
    """The suffix of this route function.

    This is set to an empty string when the function has no suffix.
    """

    def __init__(
        self, method: str, source_info: str, function_name: str, internal: bool
    ):
        self.method = method
        self.source_info = source_info
        self.function_name = function_name
        self.internal = internal
        # NOTE(CaselIT): internal falcon names do not start with 'on'
        # and do not have suffix
        if function_name.startswith('on'):
            self.suffix = '_'.join(function_name.split('_')[2:])
        else:
            self.suffix = ''


class RouteInfo(_Traversable):
    """Describes a route.

    Args:
        path (str): The path of this route.
        class_name (str): The class name of the responder of this route.
        source_info (str): The source path where this responder was defined.
        methods (list[RouteMethodInfo]): List of methods defined in the route.
    """

    __visit_name__ = 'route'

    def __init__(
        self,
        path: str,
        class_name: str,
        source_info: str,
        methods: list[RouteMethodInfo],
    ):
        self.path = path
        self.class_name = class_name
        self.source_info = source_info
        self.methods = methods


class RouteReachabilityInfo(_Traversable):
    """Describes the static reachability analysis of a route.

    Args:
        uri_template (str): The URI template of the analyzed route.
        status (str): One of :attr:`REACHABLE`, :attr:`UNREACHABLE`, or
            :attr:`UNKNOWN`.
        example_path (str | None): A concrete path that reaches the route.
        reason (str | None): A human-readable explanation of why the route
            is unreachable or could not be analyzed.
    """

    __visit_name__ = 'route_reachability'

    REACHABLE = 'reachable'
    """Status indicating that an example path that reaches the route was found."""

    UNREACHABLE = 'unreachable'
    """Status indicating that the route template can never be matched."""

    UNKNOWN = 'unknown'
    """Status indicating that the template could not be analyzed statically."""

    def __init__(
        self,
        uri_template: str,
        status: str,
        example_path: str | None = None,
        reason: str | None = None,
    ):
        self.uri_template = uri_template
        self.status = status
        self.example_path = example_path
        self.reason = reason

    @property
    def is_reachable(self) -> bool:
        """``True`` when an example path that reaches the route was found."""
        return self.status == self.REACHABLE


class StaticRouteInfo(_Traversable):
    """Describes a static route.

    Args:
        path (str): The prefix of the static route.
        directory (str): The directory for the static route.
        fallback_filename (str or None): Fallback filename to serve.
    """

    __visit_name__ = 'static_route'

    def __init__(self, prefix: str, directory: str, fallback_filename: str | None):
        self.prefix = prefix
        self.directory = directory
        self.fallback_filename = fallback_filename


class SinkInfo(_Traversable):
    """Describes a sink.

    Args:
        prefix (str): The prefix of the sink.
        name (str): The name of the sink function or class.
        source_info (str): The source path where this sink was defined.
    """

    __visit_name__ = 'sink'

    def __init__(self, prefix: str, name: str, source_info: str):
        self.prefix = prefix
        self.name = name
        self.source_info = source_info


class ErrorHandlerInfo(_Traversable):
    """Describes an error handler.

    Args:
        error (str): The name of the error type.
        name (str): The name of the handler.
        source_info (str): The source path where this error handler was defined.
        internal (bool): Whether or not this is a default error handler added by
            the framework.
    """

    __visit_name__ = 'error_handler'

    def __init__(self, error: str, name: str, source_info: str, internal: bool):
        self.error = error
        self.name = name
        self.source_info = source_info
        self.internal = internal


class MiddlewareMethodInfo(_Traversable):
    """Describes a middleware method.

    Args:
        function_name (str): Name of the method.
        source_info (str): The source path of the method.
    """

    __visit_name__ = 'middleware_method'

    def __init__(self, function_name: str, source_info: str):
        self.function_name = function_name
        self.source_info = source_info
        self.internal = False  # added for compatibility with RouteMethodInfo


class MiddlewareClassInfo(_Traversable):
    """Describes a middleware class.

    Args:
        name (str): The name of the middleware class.
        source_info (str): The source path where the middleware was defined.
        methods (list[MiddlewareMethodInfo]): List of method defined by the
            middleware class.
    """

    __visit_name__ = 'middleware_class'

    def __init__(
        self, name: str, source_info: str, methods: list[MiddlewareMethodInfo]
    ):
        self.name = name
        self.source_info = source_info
        self.methods = methods


class MiddlewareTreeItemInfo(_Traversable):
    """Describes a middleware tree entry.

    Args:
        name (str): The name of the method.
        class_name (str): The class name of the method.
    """

    __visit_name__ = 'middleware_tree_item'

    _symbols = {
        'process_request': '→',
        'process_resource': '↣',
        'process_response': '↢',
    }

    def __init__(self, name: str, class_name: str):
        self.name = name
        self.class_name = class_name


class MiddlewareTreeInfo(_Traversable):
    """Describes the middleware methods used by the app.

    Args:
        request (list[MiddlewareTreeItemInfo]): The `process_request` methods.
        resource (list[MiddlewareTreeItemInfo]): The `process_resource` methods.
        response (list[MiddlewareTreeItemInfo]): The `process_response` methods.
    """

    __visit_name__ = 'middleware_tree'

    def __init__(
        self,
        request: list[MiddlewareTreeItemInfo],
        resource: list[MiddlewareTreeItemInfo],
        response: list[MiddlewareTreeItemInfo],
    ):
        self.request = request
        self.resource = resource
        self.response = response


class MiddlewareInfo(_Traversable):
    """Describes the middleware of the app.

    Args:
        middlewareTree (MiddlewareTreeInfo): The middleware tree of the app.
        middlewareClasses (list[MiddlewareClassInfo]): The middleware classes of
            the app.
        independent (bool): Whether or not the middleware components are executed
            independently.

    """

    __visit_name__ = 'middleware'

    independent_text: str
    """Text created from the `independent` arg."""

    def __init__(
        self,
        middleware_tree: MiddlewareTreeInfo,
        middleware_classes: list[MiddlewareClassInfo],
        independent: bool,
    ):
        self.middleware_tree = middleware_tree
        self.middleware_classes = middleware_classes
        self.independent = independent

        if independent:
            self.independent_text = 'Middleware are independent'
        else:
            self.independent_text = 'Middleware are dependent'


class AppInfo(_Traversable):
    """Describes an application.

    Args:
        routes (list[RouteInfo]): The routes of the application.
        middleware (MiddlewareInfo): The middleware information in the application.
        static_routes (list[StaticRouteInfo]): The static routes of this application.
        sinks (list[SinkInfo]): The sinks of this application.
        error_handlers (list[ErrorHandlerInfo]): The error handlers of this application.
        asgi (bool): Whether or not this is an ASGI application.
    """

    __visit_name__ = 'app'

    def __init__(
        self,
        routes: list[RouteInfo],
        middleware: MiddlewareInfo,
        static_routes: list[StaticRouteInfo],
        sinks: list[SinkInfo],
        error_handlers: list[ErrorHandlerInfo],
        asgi: bool,
    ):
        self.routes = routes
        self.middleware = middleware
        self.static_routes = static_routes
        self.sinks = sinks
        self.error_handlers = error_handlers
        self.asgi = asgi

    def to_string(
        self, verbose: bool = False, internal: bool = False, name: str = ''
    ) -> str:
        """Return a string representation of this class.

        Args:
            verbose (bool, optional): Adds more information. Defaults to False.
            internal (bool, optional): Also include internal falcon route methods
                and error handlers. Defaults to ``False``.
            name (str, optional): The name of the application, to be output at the
                beginning of the text. Defaults to ``'Falcon App'``.
        Returns:
            str: A string representation of the application.
        """
        return StringVisitor(verbose, internal, name).process(self)


# ------------------------------------------------------------------------
# Visitor classes
# ------------------------------------------------------------------------


class InspectVisitor:
    """Base visitor class that implements the `process` method.

    Subclasses must implement ``visit_<name>`` methods for each supported class.
    """

    def process(self, instance: _Traversable) -> str:
        """Process the instance, by calling the appropriate visit method.

        Uses the `__visit_name__` attribute of the `instance` to obtain the method
        to use.

        Args:
            instance (_Traversable): The instance to process.
        """
        try:
            # TODO(0xMattB): Implement advanced typing to type as 'str' (see PR #2599)
            return getattr(  # type: ignore[no-any-return]
                self, 'visit_{}'.format(instance.__visit_name__)
            )(instance)
        except AttributeError as e:
            raise RuntimeError(
                'This visitor does not support {}'.format(type(instance))
            ) from e


class StringVisitor(InspectVisitor):
    """Visitor that returns a string representation of the info class.

    This is used automatically by calling ``to_string()`` on the info class.
    It can also be used directly by calling ``StringVisitor.process(info_instance)``.

    Args:
        verbose (bool, optional): Adds more information. Defaults to ``False``.
        internal (bool, optional): Also include internal route methods
            and error handlers added by the framework. Defaults to ``False``.
        name (str, optional): The name of the application, to be output at the
            beginning of the text. Defaults to ``'Falcon App'``.
    """

    def __init__(
        self, verbose: bool = False, internal: bool = False, name: str = ''
    ) -> None:
        self.verbose = verbose
        self.internal = internal
        self.name = name
        self.indent = 0

    @property
    def tab(self) -> str:
        """Get the current tabulation."""
        return ' ' * self.indent

    def visit_route_method(self, route_method: RouteMethodInfo) -> str:
        """Visit a RouteMethodInfo instance. Usually called by `process`."""
        text = '{0.method} - {0.function_name}'.format(route_method)
        if self.verbose:
            text += ' ({0.source_info})'.format(route_method)
        return text

    def _methods_to_string(
        self, methods: list[RouteMethodInfo] | list[MiddlewareMethodInfo]
    ) -> str:
        """Return a string from the list of methods."""
        tab = self.tab + ' ' * 3
        filtered_methods = _filter_internal(methods, self.internal)
        if not filtered_methods:
            return ''
        text_list = [self.process(m) for m in filtered_methods]
        method_text = ['{}├── {}'.format(tab, m) for m in text_list[:-1]]
        method_text += ['{}└── {}'.format(tab, m) for m in text_list[-1:]]
        return '\n'.join(method_text)

    def visit_route(self, route: RouteInfo) -> str:
        """Visit a RouteInfo instance. Usually called by `process`."""
        text = '{0}⇒ {1.path} - {1.class_name}'.format(self.tab, route)
        if self.verbose:
            text += ' ({0.source_info})'.format(route)

        method_text = self._methods_to_string(route.methods)
        if not method_text:
            return text

        return '{}:\n{}'.format(text, method_text)

    def visit_route_reachability(self, info: RouteReachabilityInfo) -> str:
        """Visit a RouteReachabilityInfo instance. Usually called by `process`."""
        text = '{}⇒ {}'.format(self.tab, info.uri_template)
        if info.status == RouteReachabilityInfo.REACHABLE:
            text += ' → reachable (e.g. {})'.format(info.example_path)
        elif info.status == RouteReachabilityInfo.UNREACHABLE:
            text += ' → unreachable: {}'.format(info.reason)
        else:
            text += ' → unknown: {}'.format(info.reason)
        return text

    def visit_static_route(self, static_route: StaticRouteInfo) -> str:
        """Visit a StaticRouteInfo instance. Usually called by `process`."""
        text = '{0}↦ {1.prefix} {1.directory}'.format(self.tab, static_route)
        if static_route.fallback_filename:
            text += ' [{0.fallback_filename}]'.format(static_route)
        return text

    def visit_sink(self, sink: SinkInfo) -> str:
        """Visit a SinkInfo instance. Usually called by `process`."""
        text = '{0}⇥ {1.prefix} {1.name}'.format(self.tab, sink)
        if self.verbose:
            text += ' ({0.source_info})'.format(sink)
        return text

    def visit_error_handler(self, error_handler: ErrorHandlerInfo) -> str:
        """Visit a ErrorHandlerInfo instance. Usually called by `process`."""
        text = '{0}⇜ {1.error} {1.name}'.format(self.tab, error_handler)
        if self.verbose:
            text += ' ({0.source_info})'.format(error_handler)
        return text

    def visit_middleware_method(self, middleware_method: MiddlewareMethodInfo) -> str:
        """Visit a MiddlewareMethodInfo instance. Usually called by `process`."""
        text = '{0.function_name}'.format(middleware_method)
        if self.verbose:
            text += ' ({0.source_info})'.format(middleware_method)
        return text

    def visit_middleware_class(self, middleware_class: MiddlewareClassInfo) -> str:
        """Visit a ErrorHandlerInfo instance. Usually called by `process`."""
        text = '{0}↣ {1.name}'.format(self.tab, middleware_class)
        if self.verbose:
            text += ' ({0.source_info})'.format(middleware_class)

        method_text = self._methods_to_string(middleware_class.methods)
        if not method_text:
            return text

        return '{}:\n{}'.format(text, method_text)

    def visit_middleware_tree_item(self, mti: MiddlewareTreeItemInfo) -> str:
        """Visit a MiddlewareTreeItemInfo instance. Usually called by `process`."""
        symbol = mti._symbols.get(mti.name, '→')
        return '{0}{1} {2.class_name}.{2.name}'.format(self.tab, symbol, mti)

    def visit_middleware_tree(self, m_tree: MiddlewareTreeInfo) -> str:
        """Visit a MiddlewareTreeInfo instance. Usually called by `process`."""
        before = len(m_tree.request) + len(m_tree.resource)
        after = len(m_tree.response)

        if before + after == 0:
            return ''

        each = 2
        initial = self.indent
        if after > before:
            self.indent += each * (after - before)

        text = []
        for r in m_tree.request:
            text.append(self.process(r))
            self.indent += each
        if text:
            text.append('')
        for r in m_tree.resource:
            text.append(self.process(r))
            self.indent += each

        if m_tree.resource or not text:
            text.append('')
        self.indent += each
        text.append('{}├── Process route responder'.format(self.tab))
        self.indent -= each
        if m_tree.response:
            text.append('')

        for r in m_tree.response:
            self.indent -= each
            text.append(self.process(r))

        self.indent = initial
        return '\n'.join(text)

    def visit_middleware(self, middleware: MiddlewareInfo) -> str:
        """Visit a MiddlewareInfo instance. Usually called by `process`."""
        text = self.process(middleware.middleware_tree)
        if self.verbose:
            self.indent += 4
            m_text = '\n'.join(self.process(m) for m in middleware.middleware_classes)
            self.indent -= 4
            if m_text:
                text += '\n{}- Middleware classes:\n{}'.format(self.tab, m_text)

        return text

    def visit_app(self, app: AppInfo) -> str:
        """Visit a AppInfo instance. Usually called by `process`."""

        type_ = 'ASGI' if app.asgi else 'WSGI'
        self.indent = 4
        text = '{} ({})'.format(self.name or 'Falcon App', type_)

        if app.routes:
            routes = '\n'.join(self.process(r) for r in app.routes)
            text += '\n• Routes:\n{}'.format(routes)

        middleware_text = self.process(app.middleware)
        if middleware_text:
            text += '\n• Middleware ({}):\n{}'.format(
                app.middleware.independent_text, middleware_text
            )

        if app.static_routes:
            static_routes = '\n'.join(self.process(sr) for sr in app.static_routes)
            text += '\n• Static routes:\n{}'.format(static_routes)

        if app.sinks:
            sinks = '\n'.join(self.process(s) for s in app.sinks)
            text += '\n• Sinks:\n{}'.format(sinks)

        errors = _filter_internal(app.error_handlers, self.internal)
        if errors:
            errs = '\n'.join(self.process(e) for e in errors)
            text += '\n• Error handlers:\n{}'.format(errs)

        return text


# ------------------------------------------------------------------------
# Helpers functions
# ------------------------------------------------------------------------


def _get_source_info(obj: Any, default: str | None = '[unknown file]') -> str | None:
    """Try to get the definition file and line of obj.

    Return default on error.
    """
    try:
        source_file = inspect.getsourcefile(obj)
        source_lines = inspect.findsource(obj)
        source_info = '{}:{}'.format(source_file, source_lines[1])
    except Exception:
        # NOTE(vytas): If Falcon is cythonized, all default
        # responders coming from cythonized modules will
        # appear as built-in functions, and raise a
        # TypeError when trying to locate the source file.
        return default
    return source_info


def _get_source_info_and_name(obj: Any) -> tuple[str | None, str]:
    """Attempt to get the definition file and line of obj and its name."""
    source_info = _get_source_info(obj, None)
    if source_info is None:
        # NOTE(caselit): a class instances return None. Try the type
        source_info = _get_source_info(type(obj))
    name = getattr(obj, '__name__', None)
    if name is None:
        name = getattr(type(obj), '__name__', '[unknown]')
    name = cast(str, name)
    return source_info, name


def _is_internal(obj: Any) -> bool:
    """Check if the module of the object is a falcon module."""
    module = inspect.getmodule(obj)
    if module:
        return module.__name__.startswith('falcon.')
    return False


def _filter_internal(
    iterable: Iterable[RouteMethodInfo]
    | Iterable[ErrorHandlerInfo]
    | Iterable[MiddlewareMethodInfo],
    return_internal: bool,
) -> Iterable[_Traversable] | list[_Traversable]:
    """Filter the internal elements of an iterable."""
    if return_internal:
        return iterable
    return [el for el in iterable if not el.internal]


# ------------------------------------------------------------------------
# Route reachability analysis
#
# The analysis models the matcher code generated by CompiledRouter without
# compiling it or invoking any user-provided callable. Sibling nodes are
# tested in the same order as the generated matcher: static segments
# first, complex field segments in registration order, and (at most one)
# simple field segment last.
# ------------------------------------------------------------------------

#: Result of a node analysis: (status, example segment, reason).
_NodeResult = tuple[str, Optional[str], Optional[str]]

#: A token of the simplified segment language. ``('lit', text)`` is a
#: literal run, ``('plain', '')`` a ``.+`` field, and ``('int', '')`` a
#: built-in int-converter field.
_Token = tuple[str, str]

#: Parsed ``(num_digits, min, max)`` arguments of an int converter.
_IntArgs = tuple[Optional[int], Optional[int], Optional[int]]

#: NFA transition label; ``None`` represents an epsilon transition.
_Label = Optional[Union[str, tuple[str, str]]]

#: Symbol used for Unicode decimal digits (outside the ASCII literals).
_UNICODE_DIGIT_SYM = '\x00unicode-digit'

_ALLOWED_LITERAL_RE = re.compile(r'^[A-Za-z0-9._\-]*$')

#: Arabic-Indic digits; ``int()`` accepts them but no supported literal
#: can contain them, which makes them a safe choice for int field probes.
_UNICODE_DIGITS = '٠١٢٣٤٥٦٧٨٩'

_UNKNOWN_REASON_INT_ARGS = (
    'The arguments of the int converter for field "{}" are not integer '
    'or None literals.'
)
_UNKNOWN_REASON_CONVERTER = (
    'The converter "{}" used for field "{}" cannot be analyzed statically.'
)
_UNKNOWN_REASON_LITERAL = (
    'The template contains literal characters other than ASCII letters, '
    'digits and the characters "._-".'
)
_UNKNOWN_REASON_INFLUENCE = (
    'The preceding route template "{}" may match first but uses features '
    'that cannot be analyzed statically.'
)
_UNKNOWN_REASON_UNCERTAIN = (
    'Cannot determine statically whether an int-converted field is '
    'shadowed by another template.'
)


def _reachability_walk(
    router: CompiledRouter,
    nodes: list[CompiledRouterNode],
    example_segments: list[str],
    blocked: tuple[str, str] | None,
    results: list[RouteReachabilityInfo],
) -> None:
    """Walk the router tree, propagating unreachable/unknown ancestors."""
    for node in nodes:
        node_blocked = blocked
        segments = example_segments

        if node_blocked is None:
            status, example, reason = _analyze_node(router, nodes, node)
            if status == RouteReachabilityInfo.REACHABLE:
                assert example is not None
                segments = segments + [example]
            else:
                assert reason is not None
                node_blocked = (status, reason)

        if node.resource is not None:
            uri_template = node.uri_template or ''
            if node_blocked is None:
                example_path = '/' + '/'.join(segments)
                results.append(
                    RouteReachabilityInfo(
                        uri_template,
                        RouteReachabilityInfo.REACHABLE,
                        example_path,
                    )
                )
            else:
                results.append(
                    RouteReachabilityInfo(
                        uri_template, node_blocked[0], None, node_blocked[1]
                    )
                )

        if node.children:
            _reachability_walk(router, node.children, segments, node_blocked, results)


def _analyze_node(
    router: CompiledRouter,
    siblings: list[CompiledRouterNode],
    node: CompiledRouterNode,
) -> _NodeResult:
    """Analyze a single router node against its siblings."""
    if not node.is_var:
        if not _ALLOWED_LITERAL_RE.match(node.raw_segment):
            return (
                RouteReachabilityInfo.UNKNOWN,
                None,
                _UNKNOWN_REASON_LITERAL,
            )
        return RouteReachabilityInfo.REACHABLE, node.raw_segment, None

    if not node.is_complex:
        return _analyze_simple_node(router, siblings, node)

    return _analyze_complex_node(router, siblings, node)


def _analyze_simple_node(
    router: CompiledRouter,
    siblings: list[CompiledRouterNode],
    node: CompiledRouterNode,
) -> _NodeResult:
    """Analyze a whole-segment field node such as ``{id}`` or ``{id:int}``."""
    field_name = node.var_name or ''

    if not node.var_converter_map:
        return _solve_segment(router, siblings, node, [('plain', '')], {})

    _, converter_name, argstr = node.var_converter_map[0]
    converter_class = router._converter_map.get(converter_name)

    if converter_name == 'int' and converter_class is converters.IntConverter:
        int_args = _parse_int_args(argstr)
        if int_args is None:
            return (
                RouteReachabilityInfo.UNKNOWN,
                None,
                _UNKNOWN_REASON_INT_ARGS.format(field_name),
            )
        if _int_example_value(int_args) is None:
            return (
                RouteReachabilityInfo.UNREACHABLE,
                None,
                _infeasible_int_reason(field_name),
            )
        return _solve_segment(
            router, siblings, node, [('int', '')], {field_name: int_args}
        )

    if converter_name == 'path' and converter_class is converters.PathConverter:
        # add_route rejects a path converter that is not the final field,
        # so the node is a leaf here. It consumes the whole remainder and
        # never fails; picking one remainder segment models it as a plain
        # whole-segment field.
        return _solve_segment(router, siblings, node, [('plain', '')], {})

    return (
        RouteReachabilityInfo.UNKNOWN,
        None,
        _UNKNOWN_REASON_CONVERTER.format(converter_name, field_name),
    )


def _analyze_complex_node(
    router: CompiledRouter,
    siblings: list[CompiledRouterNode],
    node: CompiledRouterNode,
) -> _NodeResult:
    """Analyze a complex field segment such as ``{id}.json``."""
    model = _model_complex(router, node)
    if model is None:
        return (
            RouteReachabilityInfo.UNKNOWN,
            None,
            _complex_unknown_reason(router, node),
        )

    tokens, int_fields = model
    for field_name, int_args in int_fields.items():
        if _int_example_value(int_args) is None:
            return (
                RouteReachabilityInfo.UNREACHABLE,
                None,
                _infeasible_int_reason(field_name),
            )

    return _solve_segment(router, siblings, node, tokens, int_fields)


def _model_complex(
    router: CompiledRouter, node: CompiledRouterNode
) -> tuple[list[_Token], dict[str, _IntArgs]] | None:
    """Convert a complex node into tokens and int args, or ``None``."""
    tokens: list[_Token] = []
    int_fields: dict[str, _IntArgs] = {}
    position = 0

    for match in _FIELD_PATTERN.finditer(node.raw_segment):
        literal = node.raw_segment[position : match.start()]
        if not _ALLOWED_LITERAL_RE.match(literal):
            return None
        tokens.append(('lit', literal))

        field_name = match.group('fname')
        converter_name = match.group('cname')
        if converter_name is None:
            tokens.append(('plain', ''))
        elif (
            converter_name == 'int'
            and router._converter_map.get('int') is converters.IntConverter
        ):
            int_args = _parse_int_args(match.group('argstr'))
            if int_args is None:
                return None
            int_fields[field_name] = int_args
            tokens.append(('int', ''))
        else:
            return None

        position = match.end()

    literal = node.raw_segment[position:]
    if not _ALLOWED_LITERAL_RE.match(literal):
        return None
    tokens.append(('lit', literal))
    return tokens, int_fields


def _complex_unknown_reason(router: CompiledRouter, node: CompiledRouterNode) -> str:
    """Produce the most specific unknown reason for an unsupported node."""
    position = 0
    for match in _FIELD_PATTERN.finditer(node.raw_segment):
        literal = node.raw_segment[position : match.start()]
        if not _ALLOWED_LITERAL_RE.match(literal):
            return _UNKNOWN_REASON_LITERAL
        field_name = match.group('fname')
        converter_name = match.group('cname')
        if converter_name is not None:
            if (
                converter_name == 'int'
                and router._converter_map.get('int') is converters.IntConverter
                and _parse_int_args(match.group('argstr')) is not None
            ):
                pass
            elif converter_name == 'int':
                return _UNKNOWN_REASON_INT_ARGS.format(field_name)
            else:
                return _UNKNOWN_REASON_CONVERTER.format(converter_name, field_name)
        position = match.end()
    return _UNKNOWN_REASON_LITERAL


def _solve_segment(
    router: CompiledRouter,
    siblings: list[CompiledRouterNode],
    node: CompiledRouterNode,
    tokens: list[_Token],
    int_fields: dict[str, _IntArgs],
) -> _NodeResult:
    """Find an example segment for a modeled node, or prove it unreachable."""
    index = siblings.index(node)

    if node.is_complex:
        earlier_complex = [
            sibling
            for sibling_index, sibling in enumerate(siblings)
            if sibling_index < index and sibling.is_var and sibling.is_complex
        ]
    else:
        # The simple field sibling is always tested last by the matcher.
        earlier_complex = [
            sibling
            for sibling in siblings
            if sibling is not node and sibling.is_var and sibling.is_complex
        ]

    earlier_models: list[
        tuple[CompiledRouterNode, list[_Token], dict[str, _IntArgs]]
    ] = []
    for earlier in earlier_complex:
        model = _model_complex(router, earlier)
        if model is None:
            return (
                RouteReachabilityInfo.UNKNOWN,
                None,
                _UNKNOWN_REASON_INFLUENCE.format(_node_template(earlier)),
            )
        earlier_models.append((earlier, model[0], model[1]))

    static_literals = {
        sibling.raw_segment for sibling in siblings if not sibling.is_var
    }

    symbols, representative = _build_symbols(
        tokens, [model[1] for model in earlier_models]
    )
    plain_char = next(iter(representative))

    t_dfa = _determinize(_build_nfa([tokens]), symbols)
    e_dfa = _determinize(_build_nfa([model[1] for model in earlier_models]), symbols)

    # Prefer short, human-readable words from the modeled difference
    # language (e.g. "a.a" for a ``{a}.{b}`` segment). The enumeration is
    # best-effort and lazily truncated, so this loop is bounded.
    difference = _shortest_difference_word(t_dfa, e_dfa, symbols)
    if difference is not None:
        for word_symbols in _iter_difference_words(
            t_dfa, e_dfa, symbols, len(difference), len(difference) + 8
        ):
            word = ''.join(representative[sym] for sym in word_symbols)
            result = _verify_word(
                word,
                node,
                tokens,
                int_fields,
                earlier_models,
                static_literals,
            )
            if result is not None:
                return RouteReachabilityInfo.REACHABLE, result, None

    # Synthetic words use Arabic-Indic digit runs that cannot occur in
    # supported literals, covering int bounds readable enumeration misses
    # (e.g. very long or sign-padded values).
    for word in _synthetic_words(tokens, int_fields, representative, plain_char):
        if not _is_model_difference(word, representative, t_dfa, e_dfa):
            continue
        result = _verify_word(
            word,
            node,
            tokens,
            int_fields,
            earlier_models,
            static_literals,
        )
        if result is not None:
            return RouteReachabilityInfo.REACHABLE, result, None

    has_int = bool(int_fields) or any(model[2] for model in earlier_models)
    if not has_int:
        return (
            RouteReachabilityInfo.UNREACHABLE,
            None,
            _shadow_reason(earlier_models),
        )

    # A difference against plain-field earlier routes alone would be a
    # sound proof: the int-field NFA is a superset of the true int
    # converter language, while plain fields are modeled exactly.
    plain_models = [model for model in earlier_models if not model[2]]
    plain_dfa = _determinize(_build_nfa([model[1] for model in plain_models]), symbols)
    if _shortest_difference_word(t_dfa, plain_dfa, symbols) is None:
        return (
            RouteReachabilityInfo.UNREACHABLE,
            None,
            _shadow_reason(plain_models),
        )

    # The modeled difference is empty but earlier int converters may
    # reject a synthetic word at runtime (greedy group assignment plus
    # conversion happens only once, without backtracking).
    for word in _synthetic_words(tokens, int_fields, representative, plain_char):
        if (
            _true_accept(node, tokens, int_fields, word)
            and not any(
                _true_accept(earlier, etokens, eint, word)
                for earlier, etokens, eint in earlier_models
            )
            and word not in static_literals
        ):
            return RouteReachabilityInfo.REACHABLE, word, None

    return (
        RouteReachabilityInfo.UNKNOWN,
        None,
        _UNKNOWN_REASON_UNCERTAIN,
    )


def _verify_word(
    word: str,
    node: CompiledRouterNode,
    tokens: list[_Token],
    int_fields: dict[str, _IntArgs],
    earlier_models: list[tuple[CompiledRouterNode, list[_Token], dict[str, _IntArgs]]],
    static_literals: set[str],
) -> str | None:
    """Return the word when it is accepted by the target and no earlier."""
    if word in static_literals:
        return None
    if not _true_accept(node, tokens, int_fields, word):
        return None
    for earlier, etokens, eint in earlier_models:
        if _true_accept(earlier, etokens, eint, word):
            return None
    return word


def _parse_int_args(argstr: str | None) -> _IntArgs | None:
    """Parse int converter arguments as integer/None literals.

    The argument expression is parsed as AST only; it is never evaluated,
    so user code cannot be executed.
    """
    if argstr is None:
        return None, None, None

    try:
        expression = ast.parse('f({})'.format(argstr), mode='eval')
    except (SyntaxError, ValueError):
        # ValueError covers embedded NUL bytes and other malformed
        # source; either way the arguments cannot be classified safely.
        return None

    call = expression.body
    if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
        return None
    if any(keyword.arg is None for keyword in call.keywords):
        return None

    names = ('num_digits', 'min', 'max')
    values: dict[str, int | None] = {}

    if len(call.args) > len(names):
        return None
    for index, argument in enumerate(call.args):
        parsed = _literal_int_or_none(argument)
        if parsed is _BAD_LITERAL:
            return None
        values[names[index]] = parsed

    for keyword in call.keywords:
        assert keyword.arg is not None
        if keyword.arg not in names or keyword.arg in values:
            return None
        parsed = _literal_int_or_none(keyword.value)
        if parsed is _BAD_LITERAL:
            return None
        values[keyword.arg] = parsed

    return values.get('num_digits'), values.get('min'), values.get('max')


_BAD_LITERAL = object()


def _literal_int_or_none(node: ast.AST) -> Any:
    """Extract an integer/None literal, allowing a leading unary minus."""
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        value = _literal_int_or_none(node.operand)
        if isinstance(value, int) and not isinstance(value, bool):
            return -value
        return _BAD_LITERAL
    if isinstance(node, ast.Constant):
        if node.value is None:
            return None
        if isinstance(node.value, int) and not isinstance(node.value, bool):
            return node.value
    return _BAD_LITERAL


def _int_example_value(int_args: _IntArgs) -> int | None:
    """Return a feasible non-negative example value, if one exists."""
    num_digits, minimum, maximum = int_args
    if minimum is not None and maximum is not None and minimum > maximum:
        return None

    low = 0 if minimum is None else max(0, minimum)

    if num_digits is None:
        if maximum is not None and low > maximum:
            # With min <= max already ruled out, maximum is negative here
            # (e.g. min=None, max=-1); render it with a leading ASCII '-'.
            return maximum
        return low

    if num_digits < 1:
        return None

    # Leading Unicode zero padding lets any shorter value reach the
    # required raw length, so no 10**(n-1) lower bound is needed here.
    top = 10**num_digits - 1
    high = top if maximum is None else min(top, maximum)
    if low <= high:
        return low

    # Negative values may fit as '-' plus at most n-1 digits.
    negative_top = 10 ** (num_digits - 1) - 1
    negative_low = -negative_top if minimum is None else max(-negative_top, minimum)
    negative_high = -1 if maximum is None else min(-1, maximum)
    if negative_low <= negative_high:
        return negative_high

    return None


def _unicode_int_word(value: int, num_digits: int | None) -> str | None:
    """Render an integer using Arabic-Indic digits to dodge literals."""
    negative = value < 0
    body = str(abs(value))
    if num_digits is None:
        width = len(body)
    else:
        width = num_digits - (1 if negative else 0)
    if len(body) > width:
        return None
    mapped = ''.join(_UNICODE_DIGITS[int(ch)] for ch in body)
    return ('-' if negative else '') + _UNICODE_DIGITS[0] * (width - len(body)) + mapped


def _int_candidate_values(int_args: _IntArgs) -> list[int]:
    """Small stable set of feasible values for an int field."""
    num_digits, minimum, maximum = int_args
    example = _int_example_value(int_args)
    candidates: list[int] = []
    for value in (
        example,
        0,
        1,
        None if example is None else example + 1,
    ):
        if value is None:
            continue
        if minimum is not None and value < minimum:
            continue
        if maximum is not None and value > maximum:
            continue
        # A feasible value always renders within num_digits; the guard
        # merely avoids wasting a probe when the args are infeasible.
        if _unicode_int_word(value, num_digits) is None:
            continue
        if value not in candidates:
            candidates.append(value)
    return candidates


def _synthetic_words(
    tokens: list[_Token],
    int_fields: dict[str, _IntArgs],
    representative: dict[str, str],
    plain_char: str,
) -> list[str]:
    """Build probe words from filler characters and Unicode digit runs."""
    field_index = 0
    int_args_list = list(int_fields.values())
    options: list[list[str]] = []

    for kind, _value in tokens:
        if kind == 'lit':
            options.append([''])
        elif kind == 'plain':
            options.append([plain_char])
        else:
            int_args = int_args_list[field_index]
            field_index += 1
            rendered = [
                word
                for value in _int_candidate_values(int_args)
                if (word := _unicode_int_word(value, int_args[0])) is not None
            ]
            options.append(rendered or [_UNICODE_DIGITS[0]])

    words: list[str] = []
    combinations: list[str] = ['']
    for token_index, (kind, value) in enumerate(tokens):
        if kind == 'lit':
            combinations = [prefix + value for prefix in combinations]
        else:
            combinations = [
                prefix + option
                for prefix in combinations
                for option in options[token_index]
            ]
    words.extend(combinations[:100])
    return words


def _true_accept(
    node: CompiledRouterNode,
    tokens: list[_Token],
    int_fields: dict[str, _IntArgs],
    word: str,
) -> bool:
    """Check runtime acceptance without invoking user code.

    Complex nodes reuse the framework-compiled ``.+`` regex (pure
    ``re`` machinery, no user callable) and our own int predicate.
    """
    if node.is_complex:
        pattern = node.var_pattern
        assert pattern is not None  # complex nodes compile their pattern
        match = pattern.match(word)
        if match is None:
            return False
        for field_name, int_args in int_fields.items():
            if _int_convert(match.group(field_name), int_args) is None:
                return False
        return True

    kind = tokens[0][0]
    if kind == 'int':
        int_args = next(iter(int_fields.values()))
        return _int_convert(word, int_args) is not None
    return True


def _int_convert(value: str, int_args: _IntArgs) -> int | None:
    """Mirror :class:`falcon.routing.IntConverter.convert` exactly."""
    num_digits, minimum, maximum = int_args
    if num_digits is not None and len(value) != num_digits:
        return None
    if value.strip() != value:
        return None
    try:
        converted = int(value)
    except ValueError:
        return None
    if minimum is not None and converted < minimum:
        return None
    if maximum is not None and converted > maximum:
        return None
    return converted


def _build_symbols(
    target_tokens: list[_Token],
    earlier_token_lists: list[list[_Token]],
) -> tuple[list[str], dict[str, str]]:
    """Build the partition alphabet and representative characters.

    Besides the literal characters and the Unicode-digit sentinel, the
    alphabet contains a few neutral filler letters and digits that no
    supported literal at this level uses. They make generated example
    paths (such as ``a.a`` or ``10``) readable while remaining
    indistinguishable from any other character for the ``.+`` field
    language.
    """
    literal_chars: set[str] = set()
    has_int = False
    for tokens in [target_tokens, *earlier_token_lists]:
        for kind, value in tokens:
            if kind == 'lit':
                literal_chars.update(value)
            elif kind == 'int':
                has_int = True

    filler_letters = [
        char for char in 'abcdefghijklmnopqrstuvwxyz' if char not in literal_chars
    ][:2]
    # All ten digits are distinct symbols so that int fields can be
    # rendered with ordinary ASCII values such as "10" or "01".
    filler_digits = [char for char in '0123456789' if char not in literal_chars]
    literal_letters = sorted(c for c in literal_chars if c.isalpha())
    literal_digits = sorted(c for c in literal_chars if c.isdigit())
    literal_punctuation = sorted(c for c in literal_chars if not c.isalnum())

    symbols = list(filler_letters)
    symbols.extend(literal_letters)
    symbols.extend(literal_digits)
    symbols.extend(filler_digits)
    symbols.extend(literal_punctuation)
    if has_int:
        for extra in ('+', '-', '_'):
            if extra not in symbols:
                symbols.append(extra)
        symbols.append(_UNICODE_DIGIT_SYM)

    representative = {char: char for char in symbols if len(char) == 1}
    representative[_UNICODE_DIGIT_SYM] = _UNICODE_DIGITS[0]
    return symbols, representative


def _label_matches(label: _Label, symbol: str) -> bool:
    """Check whether a partition symbol satisfies an NFA transition label."""
    if isinstance(label, tuple):
        return symbol == label[1]
    if label == 'any':
        return True
    # intchar: ASCII digits (including those shared with literals), the
    # sign/underscore characters, or a Unicode decimal digit.
    if symbol == _UNICODE_DIGIT_SYM:
        return True
    return symbol in '0123456789+-_'


def _build_nfa(
    token_lists: list[list[_Token]],
) -> tuple[list[list[tuple[_Label, int]]], set[int], set[int]]:
    """Build an NFA with epsilon transitions from a list of token lists."""
    transitions: list[list[tuple[_Label, int]]] = [[]]
    start_states = {0}
    accept_states: set[int] = set()

    def new_state() -> int:
        transitions.append([])
        return len(transitions) - 1

    for tokens in token_lists:
        pattern_start = new_state()
        transitions[0].append((None, pattern_start))
        current = pattern_start

        for kind, value in tokens:
            if kind == 'lit':
                for char in value:
                    nxt = new_state()
                    transitions[current].append((('char', char), nxt))
                    current = nxt
            else:
                middle = new_state()
                end = new_state()
                label = 'any' if kind == 'plain' else 'intchar'
                transitions[current].append((label, middle))
                transitions[middle].append((label, middle))
                transitions[middle].append((None, end))
                current = end

        accept_states.add(current)

    return transitions, start_states, accept_states


def _epsilon_closure(
    transitions: list[list[tuple[_Label, int]]], states: set[int]
) -> frozenset[int]:
    """Compute the epsilon closure of a set of NFA states."""
    closure = set(states)
    stack = list(states)
    while stack:
        state = stack.pop()
        for label, target in transitions[state]:
            if label is None and target not in closure:
                closure.add(target)
                stack.append(target)
    return frozenset(closure)


def _determinize(
    nfa: tuple[list[list[tuple[_Label, int]]], set[int], set[int]],
    symbols: list[str],
) -> tuple[
    frozenset[int],
    dict[tuple[frozenset[int], str], frozenset[int]],
    set[frozenset[int]],
]:
    """Subset-construct a DFA from the pattern NFA."""
    transitions, start_states, accept_states = nfa
    start = _epsilon_closure(transitions, start_states)
    dfa: dict[tuple[frozenset[int], str], frozenset[int]] = {}
    accepting: set[frozenset[int]] = set()
    queue = deque([start])
    seen = {start}

    while queue:
        states = queue.popleft()
        if states & accept_states:
            accepting.add(states)
        for symbol in symbols:
            moved: set[int] = set()
            for state in states:
                for label, target in transitions[state]:
                    if label is not None and _label_matches(label, symbol):
                        moved.add(target)
            if not moved:
                continue
            nxt = _epsilon_closure(transitions, moved)
            dfa[(states, symbol)] = nxt
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)

    return start, dfa, accepting


def _shortest_difference_word(
    target_dfa: tuple[
        frozenset[int],
        dict[tuple[frozenset[int], str], frozenset[int]],
        set[frozenset[int]],
    ],
    earlier_dfa: tuple[
        frozenset[int],
        dict[tuple[frozenset[int], str], frozenset[int]],
        set[frozenset[int]],
    ],
    symbols: list[str],
) -> tuple[str, ...] | None:
    """BFS the product DFA for its shortest accepting symbol sequence."""
    t_start, t_transitions, t_accept = target_dfa
    e_start, e_transitions, e_accept = earlier_dfa
    start: tuple[frozenset[int], frozenset[int] | None] = (
        t_start,
        e_start,
    )
    queue: deque[
        tuple[
            tuple[frozenset[int], frozenset[int] | None],
            tuple[str, ...],
        ]
    ] = deque([(start, ())])
    seen = {start}

    while queue:
        (t_state, e_state), word = queue.popleft()
        for symbol in symbols:
            t_next = t_transitions.get((t_state, symbol))
            if t_next is None:
                continue
            e_next = (
                e_transitions.get((e_state, symbol)) if e_state is not None else None
            )
            nxt = (t_next, e_next)
            if nxt in seen:
                continue
            nxt_word = word + (symbol,)
            if t_next in t_accept and e_next not in e_accept:
                return nxt_word
            seen.add(nxt)
            queue.append((nxt, nxt_word))

    return None


#: Maximum number of prefixes that may enter the same product-DFA state at
#: a given remaining depth. Field loops make the raw tree exponential even
#: though only a small number of distinct state pairs exist.
_MAX_PAIR_ENTRIES = 16

#: Maximum number of words yielded by :func:`_iter_difference_words`.
_ENUMERATION_WORD_LIMIT = 1000

#: Maximum number of product-DFA transitions explored per enumeration.
_ENUMERATION_EXPANSION_BUDGET = 50000


def _iter_difference_words(
    target_dfa: tuple[
        frozenset[int],
        dict[tuple[frozenset[int], str], frozenset[int]],
        set[frozenset[int]],
    ],
    earlier_dfa: tuple[
        frozenset[int],
        dict[tuple[frozenset[int], str], frozenset[int]],
        set[frozenset[int]],
    ],
    symbols: list[str],
    min_length: int,
    max_length: int,
) -> Iterator[tuple[str, ...]]:
    """Lazily yield difference words of increasing length, symbol ordered.

    The enumeration is deliberately bounded: infinitely many words can run
    through ``.+``-style fields, and every yielded word is independently
    verified against the reconstructed matching semantics by the caller,
    so truncation can only downgrade the verdict to ``unknown``; it can
    never make a reachable or unreachable verdict unsound.
    """
    t_start, t_transitions, t_accept = target_dfa
    e_start, e_transitions, e_accept = earlier_dfa
    yielded = 0
    expansions = 0

    def recurse(
        remaining: int,
        t_state: frozenset[int],
        e_state: frozenset[int] | None,
        prefix: tuple[str, ...],
    ) -> Iterator[tuple[str, ...]]:
        nonlocal yielded, expansions
        if remaining == 0:
            if t_state in t_accept and e_state not in e_accept:
                yielded += 1
                yield prefix
            return

        key = (remaining, t_state, e_state)
        entries = entries_by_key.get(key, 0)
        if entries >= _MAX_PAIR_ENTRIES:
            return
        entries_by_key[key] = entries + 1

        for symbol in symbols:
            if (
                yielded >= _ENUMERATION_WORD_LIMIT
                or expansions >= _ENUMERATION_EXPANSION_BUDGET
            ):
                return
            t_next = t_transitions.get((t_state, symbol))
            if t_next is None:
                continue
            expansions += 1
            e_next = None if e_state is None else e_transitions.get((e_state, symbol))
            yield from recurse(remaining - 1, t_next, e_next, prefix + (symbol,))

    for length in range(min_length, max_length + 1):
        entries_by_key: dict[
            tuple[int, frozenset[int], frozenset[int] | None], int
        ] = {}
        yield from recurse(length, t_start, e_start, ())
        if (
            yielded >= _ENUMERATION_WORD_LIMIT
            or expansions >= _ENUMERATION_EXPANSION_BUDGET
        ):
            return


def _is_model_difference(
    word: str,
    representative: dict[str, str],
    target_dfa: tuple[
        frozenset[int],
        dict[tuple[frozenset[int], str], frozenset[int]],
        set[frozenset[int]],
    ],
    earlier_dfa: tuple[
        frozenset[int],
        dict[tuple[frozenset[int], str], frozenset[int]],
        set[frozenset[int]],
    ],
) -> bool:
    """Check whether a concrete word lies in the modeled difference."""
    char_to_symbol = {char: sym for sym, char in representative.items()}
    t_start, t_transitions, t_accept = target_dfa
    e_start, e_transitions, e_accept = earlier_dfa

    t_state: frozenset[int] | None = t_start
    e_state: frozenset[int] | None = e_start
    for char in word:
        symbol = char_to_symbol.get(char)
        if symbol is None:
            return False
        assert t_state is not None
        t_state = t_transitions.get((t_state, symbol))
        if t_state is None:
            return False
        e_state = None if e_state is None else e_transitions.get((e_state, symbol))
    assert t_state is not None
    return t_state in t_accept and e_state not in e_accept


def _infeasible_int_reason(field_name: str) -> str:
    """Build the reason for an int field that can never match."""
    return (
        'The int converter constraints for field "{}" can never be '
        'satisfied by any integer value.'.format(field_name)
    )


def _shadow_reason(
    earlier_models: list[tuple[CompiledRouterNode, list[_Token], dict[str, _IntArgs]]],
) -> str:
    """Build the reason for a template shadowed by an earlier route."""
    if earlier_models:
        template = _node_template(earlier_models[0][0])
        return (
            'Every matching path is consumed by the route registered '
            'earlier as "{}".'.format(template)
        )
    return 'The route template is shadowed by an earlier sibling route.'


def _node_template(node: CompiledRouterNode) -> str:
    """Return a representative route URI template passing through a node."""
    if node.uri_template:
        return node.uri_template
    queue = deque(node.children)
    while queue:
        current = queue.popleft()
        if current.uri_template:
            return current.uri_template
        queue.extend(current.children)
    return node.raw_segment
