from functools import partial
import itertools
import os
import pathlib
import sys
import types

import _inspect_fixture as i_f
import pytest

import falcon
from falcon import inspect
from falcon import routing
import falcon.asgi

HERE = pathlib.Path(__file__).resolve().parent


def get_app(asgi, cors=True, **kw):
    if asgi:
        return falcon.asgi.App(cors_enable=cors, **kw)
    else:
        return falcon.App(cors_enable=cors, **kw)


def make_app():
    app = get_app(False, cors=True)
    app.add_middleware(i_f.MyMiddleware())
    app.add_middleware(i_f.OtherMiddleware())

    app.add_sink(i_f.sinkFn, '/sink_fn')
    app.add_sink(i_f.SinkClass(), '/sink_cls')

    app.add_error_handler(RuntimeError, i_f.my_error_handler)

    app.add_route('/foo', i_f.MyResponder())
    app.add_route('/foo/{id}', i_f.MyResponder(), suffix='id')
    app.add_route('/bar', i_f.OtherResponder(), suffix='id')

    app.add_static_route('/fal', os.path.abspath('falcon'))
    app.add_static_route('/tes', HERE, fallback_filename='conftest.py')
    return app


def make_app_async():
    app = get_app(True, cors=True)
    app.add_middleware(i_f.MyMiddlewareAsync())
    app.add_middleware(i_f.OtherMiddlewareAsync())

    app.add_sink(i_f.sinkFn, '/sink_fn')
    app.add_sink(i_f.SinkClass(), '/sink_cls')

    app.add_error_handler(RuntimeError, i_f.my_error_handler_async)

    app.add_route('/foo', i_f.MyResponderAsync())
    app.add_route('/foo/{id}', i_f.MyResponderAsync(), suffix='id')
    app.add_route('/bar', i_f.OtherResponderAsync(), suffix='id')

    app.add_static_route('/fal', os.path.abspath('falcon'))
    app.add_static_route('/tes', HERE, fallback_filename='conftest.py')
    return app


class TestInspectApp:
    def test_empty_app(self, asgi):
        ai = inspect.inspect_app(get_app(asgi, False))

        assert ai.routes == []
        assert ai.middleware.middleware_tree.request == []
        assert ai.middleware.middleware_tree.resource == []
        assert ai.middleware.middleware_tree.response == []
        assert ai.middleware.middleware_classes == []
        assert ai.middleware.independent is True
        assert ai.static_routes == []
        assert ai.sinks == []
        assert len(ai.error_handlers) == 4 if asgi else 3
        assert ai.asgi is asgi

    def test_dependent_middleware(self, asgi):
        app = get_app(asgi, cors=False, independent_middleware=False)
        ai = inspect.inspect_app(app)
        assert ai.middleware.independent is False

    def test_app(self, asgi):
        ai = inspect.inspect_app(make_app_async() if asgi else make_app())

        assert len(ai.routes) == 3
        assert len(ai.middleware.middleware_tree.request) == 2
        assert len(ai.middleware.middleware_tree.resource) == 1
        assert len(ai.middleware.middleware_tree.response) == 3
        assert len(ai.middleware.middleware_classes) == 3
        assert len(ai.static_routes) == 2
        assert len(ai.sinks) == 2
        assert len(ai.error_handlers) == 5 if asgi else 4
        assert ai.asgi is asgi

    def check_route(self, asgi, r, p, cn, ml, fnt):
        assert isinstance(r, inspect.RouteInfo)
        assert r.path == p
        if asgi:
            cn += 'Async'
        assert r.class_name == cn
        assert '_inspect_fixture.py' in r.source_info

        for m in r.methods:
            assert isinstance(m, inspect.RouteMethodInfo)
            internal = '_inspect_fixture.py' not in m.source_info
            assert m.internal is internal
            if not internal:
                assert m.method in ml
                assert '_inspect_fixture.py' in m.source_info
                assert m.function_name == fnt.format(m.method).lower()

    def test_routes(self, asgi):
        routes = inspect.inspect_routes(make_app_async() if asgi else make_app())

        self.check_route(
            asgi, routes[0], '/foo', 'MyResponder', ['GET', 'POST', 'DELETE'], 'on_{}'
        )
        self.check_route(
            asgi,
            routes[1],
            '/foo/{id}',
            'MyResponder',
            ['GET', 'PUT', 'DELETE'],
            'on_{}_id',
        )
        self.check_route(
            asgi, routes[2], '/bar', 'OtherResponder', ['POST'], 'on_{}_id'
        )

    def test_routes_empty_paths(self, asgi):
        app = get_app(asgi)
        r = i_f.MyResponderAsync() if asgi else i_f.MyResponder()
        app.add_route('/foo/bar/baz', r)

        routes = inspect.inspect_routes(app)

        assert len(routes) == 1

        self.check_route(
            asgi,
            routes[0],
            '/foo/bar/baz',
            'MyResponder',
            ['GET', 'POST', 'DELETE'],
            'on_{}',
        )

    def test_static_routes(self, asgi):
        routes = inspect.inspect_static_routes(make_app_async() if asgi else make_app())

        assert all(isinstance(sr, inspect.StaticRouteInfo) for sr in routes)
        assert routes[-1].prefix == '/fal/'
        assert routes[-1].directory == os.path.abspath('falcon')
        assert routes[-1].fallback_filename is None
        assert routes[-2].prefix == '/tes/'
        assert routes[-2].directory == str(HERE)
        assert routes[-2].fallback_filename.endswith('conftest.py')

    def test_sink(self, asgi):
        sinks = inspect.inspect_sinks(make_app_async() if asgi else make_app())

        assert all(isinstance(s, inspect.SinkInfo) for s in sinks)
        assert sinks[-1].prefix == '/sink_fn'
        assert sinks[-1].name == 'sinkFn'
        if not asgi:
            assert '_inspect_fixture.py' in sinks[-1].source_info
        assert sinks[-2].prefix == '/sink_cls'
        assert sinks[-2].name == 'SinkClass'
        if not asgi:
            assert '_inspect_fixture.py' in sinks[-2].source_info

    @pytest.mark.skipif(sys.version_info < (3, 6), reason='dict order is not stable')
    def test_error_handler(self, asgi):
        errors = inspect.inspect_error_handlers(
            make_app_async() if asgi else make_app()
        )

        assert all(isinstance(e, inspect.ErrorHandlerInfo) for e in errors)
        assert errors[-1].error == 'RuntimeError'
        assert (
            errors[-1].name == 'my_error_handler_async' if asgi else 'my_error_handler'
        )
        assert '_inspect_fixture.py' in errors[-1].source_info
        assert errors[-1].internal is False
        for eh in errors[:-1]:
            assert eh.internal
            assert eh.error in (
                'WebSocketDisconnected',
                'Exception',
                'HTTPStatus',
                'HTTPError',
            )

    def test_middleware(self, asgi):
        mi = inspect.inspect_middleware(make_app_async() if asgi else make_app())

        def test(m, cn, ml, inte):
            assert isinstance(m, inspect.MiddlewareClassInfo)
            assert m.name == cn
            if inte:
                assert '_inspect_fixture.py' not in m.source_info
            else:
                assert '_inspect_fixture.py' in m.source_info

            for mm in m.methods:
                assert isinstance(mm, inspect.MiddlewareMethodInfo)
                if inte:
                    assert '_inspect_fixture.py' not in mm.source_info
                else:
                    assert '_inspect_fixture.py' in mm.source_info
                assert mm.function_name in ml

        test(
            mi.middleware_classes[0],
            'CORSMiddleware',
            ['process_response_async'] if asgi else ['process_response'],
            True,
        )
        test(
            mi.middleware_classes[1],
            'MyMiddlewareAsync' if asgi else 'MyMiddleware',
            ['process_request', 'process_resource', 'process_response'],
            False,
        )
        test(
            mi.middleware_classes[2],
            'OtherMiddlewareAsync' if asgi else 'OtherMiddleware',
            ['process_request', 'process_resource', 'process_response'],
            False,
        )

    def test_middleware_tree(self, asgi):
        mi = inspect.inspect_middleware(make_app_async() if asgi else make_app())

        def test(tl, names, cls):
            for t, n, c in zip(tl, names, cls):
                assert isinstance(t, inspect.MiddlewareTreeItemInfo)
                assert t.name == n
                assert t.class_name == c

        assert isinstance(mi.middleware_tree, inspect.MiddlewareTreeInfo)

        test(
            mi.middleware_tree.request,
            ['process_request'] * 2,
            [n + 'Async' if asgi else n for n in ['MyMiddleware', 'OtherMiddleware']],
        )
        test(
            mi.middleware_tree.resource,
            ['process_resource'],
            ['MyMiddlewareAsync' if asgi else 'MyMiddleware'],
        )
        test(
            mi.middleware_tree.response,
            [
                'process_response',
                'process_response',
                'process_response_async' if asgi else 'process_response',
            ],
            [
                'OtherMiddlewareAsync' if asgi else 'OtherMiddleware',
                'MyMiddlewareAsync' if asgi else 'MyMiddleware',
                'CORSMiddleware',
            ],
        )


def test_route_method_info_suffix():
    ri = inspect.RouteMethodInfo('foo', '', 'on_get', False)
    assert ri.suffix == ''

    ri = inspect.RouteMethodInfo('foo', '', 'on_get_suffix', False)
    assert ri.suffix == 'suffix'

    ri = inspect.RouteMethodInfo('foo', '', 'on_get_multiple_underscores_suffix', False)
    assert ri.suffix == 'multiple_underscores_suffix'

    ri = inspect.RouteMethodInfo('foo', '', 'some_other_fn_name', False)
    assert ri.suffix == ''


class TestRouter:
    def test_compiled_partial(self):
        r = routing.CompiledRouter()
        r.add_route('/foo', i_f.MyResponder())
        # override a method with a partial
        r._roots[0].method_map['GET'] = partial(r._roots[0].method_map['GET'])
        ri = inspect.inspect_compiled_router(r)

        for m in ri[0].methods:
            if m.method == 'GET':
                assert '_inspect_fixture' in m.source_info

    def test_compiled_no_method_map(self):
        r = routing.CompiledRouter()
        r.add_route('/foo', i_f.MyResponder())
        # clear the method map
        r._roots[0].method_map.clear()
        ri = inspect.inspect_compiled_router(r)

        assert ri[0].path == '/foo'
        assert ri[0].class_name == 'MyResponder'
        assert ri[0].methods == []

    def test_register_router_not_found(self, monkeypatch):
        monkeypatch.setattr(inspect, '_supported_routers', {})

        app = get_app(False)
        with pytest.raises(TypeError, match='Unsupported router class'):
            inspect.inspect_routes(app)

    def test_register_other_router(self, monkeypatch):
        monkeypatch.setattr(inspect, '_supported_routers', {})

        app = get_app(False)
        app._router = i_f.MyRouter()

        @inspect.register_router(i_f.MyRouter)
        def print_routes(r):
            assert r is app._router
            return [inspect.RouteInfo('foo', 'bar', '', [])]

        ri = inspect.inspect_routes(app)

        assert ri[0].source_info == ''
        assert ri[0].path == 'foo'
        assert ri[0].class_name == 'bar'
        assert ri[0].methods == []

    def test_register_router_multiple_time(self, monkeypatch):
        monkeypatch.setattr(inspect, '_supported_routers', {})

        @inspect.register_router(i_f.MyRouter)
        def print_routes(r):
            return []

        with pytest.raises(ValueError, match='Another function is already registered'):

            @inspect.register_router(i_f.MyRouter)
            def print_routes2(r):
                return []


def test_info_class_repr_to_string():
    ai = inspect.inspect_app(make_app())

    assert str(ai) == ai.to_string()
    assert str(ai.routes[0]) == ai.routes[0].to_string()
    assert str(ai.routes[0].methods[0]) == ai.routes[0].methods[0].to_string()
    assert str(ai.middleware) == ai.middleware.to_string()
    s = str(ai.middleware.middleware_classes[0])
    assert s == ai.middleware.middleware_classes[0].to_string()
    s = str(ai.middleware.middleware_tree.request[0])
    assert s == ai.middleware.middleware_tree.request[0].to_string()
    assert str(ai.static_routes[0]) == ai.static_routes[0].to_string()
    assert str(ai.sinks[0]) == ai.sinks[0].to_string()
    assert str(ai.error_handlers[0]) == ai.error_handlers[0].to_string()


class TestInspectVisitor:
    def test_inspect_visitor(self):
        iv = inspect.InspectVisitor()
        with pytest.raises(RuntimeError, match='This visitor does not support'):
            iv.process(123)
        with pytest.raises(RuntimeError, match='This visitor does not support'):
            iv.process(inspect.RouteInfo('f', 'o', 'o', []))

    def test_process(self):
        class FooVisitor(inspect.InspectVisitor):
            def visit_route(self, route):
                return 'foo'

        assert FooVisitor().process(inspect.RouteInfo('f', 'o', 'o', [])) == 'foo'


def test_string_visitor_class():
    assert issubclass(inspect.StringVisitor, inspect.InspectVisitor)

    sv = inspect.StringVisitor()
    assert sv.verbose is False
    assert sv.internal is False
    assert sv.name == ''


@pytest.mark.parametrize('internal', (True, False))
class TestStringVisitor:
    def test_route_method(self, internal):
        sv = inspect.StringVisitor(False, internal)
        rm = inspect.inspect_routes(make_app())[0].methods[0]

        assert sv.process(rm) == f'{rm.method} - {rm.function_name}'

    def test_route_method_verbose(self, internal):
        sv = inspect.StringVisitor(True, internal)
        rm = inspect.inspect_routes(make_app())[0].methods[0]

        assert sv.process(rm) == f'{rm.method} - {rm.function_name} ({rm.source_info})'

    def test_route(self, internal):
        sv = inspect.StringVisitor(False, internal)
        r = inspect.inspect_routes(make_app())[0]

        ml = [
            f'   ├── {sv.process(m)}' for m in r.methods if not m.internal or internal
        ][:-1]
        ml += [
            f'   └── {sv.process(m)}' for m in r.methods if not m.internal or internal
        ][-1:]

        exp = '⇒ {0.path} - {0.class_name}:\n{1}'.format(r, '\n'.join(ml))
        assert sv.process(r) == exp

    def test_route_verbose(self, internal):
        sv = inspect.StringVisitor(True, internal)
        r = inspect.inspect_routes(make_app())[0]

        ml = [
            f'   ├── {sv.process(m)}' for m in r.methods if not m.internal or internal
        ][:-1]
        ml += [
            f'   └── {sv.process(m)}' for m in r.methods if not m.internal or internal
        ][-1:]

        exp = '⇒ {0.path} - {0.class_name} ({0.source_info}):\n{1}'.format(
            r, '\n'.join(ml)
        )
        assert sv.process(r) == exp

    def test_route_no_methods(self, internal):
        sv = inspect.StringVisitor(False, internal)
        r = inspect.inspect_routes(make_app())[0]
        r.methods.clear()
        exp = f'⇒ {r.path} - {r.class_name}'
        assert sv.process(r) == exp

    @pytest.mark.parametrize('verbose', (True, False))
    def test_static_route(self, verbose, internal):
        sv = inspect.StringVisitor(verbose, internal)
        sr = inspect.inspect_static_routes(make_app())
        no_file = sr[1]
        assert sv.process(no_file) == f'↦ {no_file.prefix} {no_file.directory}'
        with_file = sr[0]
        exp = f'↦ {with_file.prefix} {with_file.directory} [{with_file.fallback_filename}]'  # noqa: E501
        assert sv.process(with_file) == exp

    def test_sink(self, internal):
        sv = inspect.StringVisitor(False, internal)
        s = inspect.inspect_sinks(make_app())[0]

        assert sv.process(s) == f'⇥ {s.prefix} {s.name}'

    def test_sink_verbose(self, internal):
        sv = inspect.StringVisitor(True, internal)
        s = inspect.inspect_sinks(make_app())[0]

        assert sv.process(s) == f'⇥ {s.prefix} {s.name} ({s.source_info})'

    def test_error_handler(self, internal):
        sv = inspect.StringVisitor(False, internal)
        e = inspect.inspect_error_handlers(make_app())[0]

        assert sv.process(e) == f'⇜ {e.error} {e.name}'

    def test_error_handler_verbose(self, internal):
        sv = inspect.StringVisitor(True, internal)
        e = inspect.inspect_error_handlers(make_app())[0]

        assert sv.process(e) == f'⇜ {e.error} {e.name} ({e.source_info})'

    def test_middleware_method(self, internal):
        sv = inspect.StringVisitor(False, internal)
        mm = inspect.inspect_middleware(make_app()).middleware_classes[0].methods[0]

        assert sv.process(mm) == f'{mm.function_name}'

    def test_middleware_method_verbose(self, internal):
        sv = inspect.StringVisitor(True, internal)
        mm = inspect.inspect_middleware(make_app()).middleware_classes[0].methods[0]

        assert sv.process(mm) == f'{mm.function_name} ({mm.source_info})'

    def test_middleware_class(self, internal):
        sv = inspect.StringVisitor(False, internal)
        mc = inspect.inspect_middleware(make_app()).middleware_classes[0]

        mml = [f'   ├── {sv.process(m)}' for m in mc.methods][:-1]
        mml += [f'   └── {sv.process(m)}' for m in mc.methods][-1:]

        exp = '↣ {.name}:\n{}'.format(mc, '\n'.join(mml))
        assert sv.process(mc) == exp

    def test_middleware_class_verbose(self, internal):
        sv = inspect.StringVisitor(True, internal)
        mc = inspect.inspect_middleware(make_app()).middleware_classes[0]

        mml = [f'   ├── {sv.process(m)}' for m in mc.methods][:-1]
        mml += [f'   └── {sv.process(m)}' for m in mc.methods][-1:]

        exp = '↣ {0.name} ({0.source_info}):\n{1}'.format(mc, '\n'.join(mml))
        assert sv.process(mc) == exp

    def test_middleware_class_no_methods(self, internal):
        sv = inspect.StringVisitor(False, internal)
        mc = inspect.inspect_middleware(make_app()).middleware_classes[0]
        mc.methods.clear()
        exp = f'↣ {mc.name}'
        assert sv.process(mc) == exp

    @pytest.mark.parametrize('verbose', (True, False))
    def test_middleware_tree_item(self, verbose, internal):
        sv = inspect.StringVisitor(verbose, internal)
        mt = inspect.inspect_middleware(make_app()).middleware_tree
        for r, s in (
            (mt.request[0], '→'),
            (mt.resource[0], '↣'),
            (mt.response[0], '↢'),
        ):
            assert sv.process(r) == f'{s} {r.class_name}.{r.name}'

    @pytest.mark.parametrize('verbose', (True, False))
    def test_middleware_tree(self, verbose, internal):
        sv = inspect.StringVisitor(verbose, internal)
        mt = inspect.inspect_middleware(make_app()).middleware_tree
        lines = []
        space = ''
        for r in mt.request:
            lines.append(space + sv.process(r))
            space += '  '
        lines.append('')
        for r in mt.resource:
            lines.append(space + sv.process(r))
            space += '  '
        lines.append('')
        lines.append(space + '  ├── Process route responder')
        lines.append('')
        for r in mt.response:
            space = space[:-2]
            lines.append(space + sv.process(r))

        assert sv.process(mt) == '\n'.join(lines)

    def test_middleware_tree_response_only(self, internal):
        sv = inspect.StringVisitor(False, internal)
        mt = inspect.inspect_middleware(make_app()).middleware_tree
        mt.request.clear()
        mt.resource.clear()
        lines = []
        space = '  ' * len(mt.response)
        lines.append('')
        lines.append(space + '  ├── Process route responder')
        lines.append('')
        for r in mt.response:
            space = space[:-2]
            lines.append(space + sv.process(r))

        assert sv.process(mt) == '\n'.join(lines)

    def test_middleware_tree_no_response(self, internal):
        sv = inspect.StringVisitor(False, internal)
        mt = inspect.inspect_middleware(make_app()).middleware_tree
        mt.response.clear()
        lines = []
        space = ''
        for r in mt.request:
            lines.append(space + sv.process(r))
            space += '  '
        lines.append('')
        for r in mt.resource:
            lines.append(space + sv.process(r))
            space += '  '
        lines.append('')
        lines.append(space + '  ├── Process route responder')

        assert sv.process(mt) == '\n'.join(lines)

    def test_middleware_tree_no_resource(self, internal):
        sv = inspect.StringVisitor(False, internal)
        mt = inspect.inspect_middleware(make_app()).middleware_tree
        mt.resource.clear()
        lines = []
        space = '  '
        for r in mt.request:
            lines.append(space + sv.process(r))
            space += '  '
        lines.append('')
        lines.append(space + '  ├── Process route responder')
        lines.append('')
        for r in mt.response:
            space = space[:-2]
            lines.append(space + sv.process(r))

        assert sv.process(mt) == '\n'.join(lines)

    def test_middleware(self, internal):
        sv = inspect.StringVisitor(False, internal)
        m = inspect.inspect_middleware(make_app())

        assert sv.process(m) == sv.process(m.middleware_tree)

    def test_middleware_verbose(self, internal):
        sv = inspect.StringVisitor(True, internal)
        m = inspect.inspect_middleware(make_app())

        mt = sv.process(m.middleware_tree)
        sv.indent += 4
        mc = '\n'.join(sv.process(cls) for cls in m.middleware_classes)
        exp = f'{mt}\n- Middleware classes:\n{mc}'
        assert inspect.StringVisitor(True).process(m) == exp

    def make(self, sv, app, v, i, r=True, m=True, sr=True, s=True, e=True):
        text = 'Falcon App (WSGI)'
        sv.indent = 4
        if r:
            text += '\n• Routes:\n{}'.format(
                '\n'.join(sv.process(r) for r in app.routes)
            )
        if m:
            mt = sv.process(app.middleware)
            text += f'\n• Middleware ({app.middleware.independent_text}):\n{mt}'
        if sr:
            sr = '\n'.join(sv.process(sr) for sr in app.static_routes)
            text += f'\n• Static routes:\n{sr}'
        if s:
            text += '\n• Sinks:\n{}'.format('\n'.join(sv.process(s) for s in app.sinks))
        if e:
            err = '\n'.join(
                sv.process(e) for e in app.error_handlers if not e.internal or i
            )
            text += f'\n• Error handlers:\n{err}'
        return text

    @pytest.mark.parametrize('verbose', (True, False))
    def test_app(self, verbose, internal):
        sv = inspect.StringVisitor(verbose, internal)
        app = inspect.inspect_app(make_app())

        assert inspect.StringVisitor(verbose, internal).process(app) == self.make(
            sv, app, verbose, internal
        )

    @pytest.mark.parametrize('verbose', (True, False))
    def test_app_no_routes(self, verbose, internal):
        sv = inspect.StringVisitor(verbose, internal)
        app = inspect.inspect_app(make_app())
        app.routes.clear()
        assert inspect.StringVisitor(verbose, internal).process(app) == self.make(
            sv, app, verbose, internal, r=False
        )

    @pytest.mark.parametrize('verbose', (True, False))
    def test_app_no_middleware(self, verbose, internal):
        sv = inspect.StringVisitor(verbose, internal)
        app = inspect.inspect_app(make_app())
        app.middleware.middleware_classes.clear()
        app.middleware.middleware_tree.request.clear()
        app.middleware.middleware_tree.resource.clear()
        app.middleware.middleware_tree.response.clear()
        assert inspect.StringVisitor(verbose, internal).process(app) == self.make(
            sv, app, verbose, internal, m=False
        )

    @pytest.mark.parametrize('verbose', (True, False))
    def test_app_static_routes(self, verbose, internal):
        sv = inspect.StringVisitor(verbose, internal)
        app = inspect.inspect_app(make_app())
        app.static_routes.clear()
        assert inspect.StringVisitor(verbose, internal).process(app) == self.make(
            sv, app, verbose, internal, sr=False
        )

    @pytest.mark.parametrize('verbose', (True, False))
    def test_app_no_sink(self, verbose, internal):
        sv = inspect.StringVisitor(verbose, internal)
        app = inspect.inspect_app(make_app())
        app.sinks.clear()
        assert inspect.StringVisitor(verbose, internal).process(app) == self.make(
            sv, app, verbose, internal, s=False
        )

    @pytest.mark.parametrize('verbose', (True, False))
    def test_app_no_errors(self, verbose, internal):
        sv = inspect.StringVisitor(verbose, internal)
        app = inspect.inspect_app(make_app())
        app.error_handlers.clear()
        assert inspect.StringVisitor(verbose, internal).process(app) == self.make(
            sv, app, verbose, internal, e=False
        )

    def test_app_name(self, internal):
        sv = inspect.StringVisitor(False, internal, name='foo')
        app = inspect.inspect_app(make_app())

        s = sv.process(app).splitlines()[0]
        assert s == 'foo (WSGI)'
        assert app.to_string(name='bar').splitlines()[0] == 'bar (WSGI)'


def test_is_internal():
    assert inspect._is_internal(1) is False
    assert inspect._is_internal(dict) is False
    assert inspect._is_internal(inspect) is True


class TestRouteReachability:
    def _app(self, asgi, routes):
        app = get_app(asgi, cors=False)
        resource = i_f.MyResponderAsync() if asgi else i_f.MyResponder()
        for route in routes:
            app.add_route(route, resource)
        return app

    def _by_template(self, infos):
        return {info.uri_template: info for info in infos}

    def _assert_examples_route(self, app, infos):
        # Using the real compiled router here is a test action; the
        # inspection itself must not trigger compilation (asserted by
        # the callers before invoking this helper).
        for info in infos:
            assert isinstance(info, inspect.RouteReachabilityInfo)
            if info.status == inspect.RouteReachabilityInfo.REACHABLE:
                found = app._router.find(info.example_path)
                assert found is not None, info.uri_template
                assert found[3] == info.uri_template, (
                    info.uri_template,
                    info.example_path,
                )
                assert info.is_reachable is True
                assert info.reason is None

    def _assert_never_routes(self, app, template, words):
        for word in words:
            found = app._router.find(word)
            assert found is None or found[3] != template, (template, word)

    def test_empty_app(self, asgi):
        app = get_app(asgi, cors=False)
        assert inspect.inspect_route_reachability(app) == []
        # Inspection does not lazily compile the router.
        assert app._router._find.__name__ == '_compile_and_find'

    def test_reachable_templates(self, asgi):
        routes = [
            '/',
            '/static',
            '/lit/a-b_c.1',
            '/simple/{id}',
            '/ints/{n:int}',
            '/bounded/{n:int(min=10, max=99)}',
            '/digits/{n:int(num_digits=3)}',
            '/positional/{n:int(2)}',
            '/trip/{n:int(3, 1, 999)}',
            '/nonearg/{n:int(None, 1)}',
            '/negatives/{n:int(min=-5, max=-1)}',
            '/open-neg/{n:int(max=-1)}',
            '/complex/{a}.{b}',
            '/intcomplex/{a:int}.json',
            '/files/{rest:path}',
            '/multi/level/{id}',
        ]
        app = self._app(asgi, routes)
        infos = inspect.inspect_route_reachability(app)
        assert app._router._find.__name__ == '_compile_and_find'

        # Idempotent on an uncompiled router.
        assert [
            (i.uri_template, i.status, i.example_path, i.reason)
            for i in inspect.inspect_route_reachability(app)
        ] == [(i.uri_template, i.status, i.example_path, i.reason) for i in infos]

        by_template = self._by_template(infos)
        expected_examples = {
            '/': '/',
            '/static': '/static',
            '/lit/a-b_c.1': '/lit/a-b_c.1',
            '/simple/{id}': '/simple/a',
            '/ints/{n:int}': '/ints/0',
            '/bounded/{n:int(min=10, max=99)}': '/bounded/10',
            '/digits/{n:int(num_digits=3)}': '/digits/000',
            '/positional/{n:int(2)}': '/positional/00',
            '/trip/{n:int(3, 1, 999)}': '/trip/001',
            '/nonearg/{n:int(None, 1)}': '/nonearg/1',
            '/negatives/{n:int(min=-5, max=-1)}': '/negatives/-1',
            '/open-neg/{n:int(max=-1)}': '/open-neg/-1',
            '/complex/{a}.{b}': '/complex/a.a',
            '/intcomplex/{a:int}.json': '/intcomplex/0.json',
            '/files/{rest:path}': '/files/a',
            '/multi/level/{id}': '/multi/level/a',
        }
        for template in routes:
            info = by_template[template]
            assert info.status == inspect.RouteReachabilityInfo.REACHABLE, (
                template,
                info.status,
                info.reason,
            )
            assert info.example_path == expected_examples[template], template
        self._assert_examples_route(app, infos)

    def test_shadowed_templates(self, asgi):
        routes = [
            '/sh/{a}.{b}',
            '/sh/{x}.json',
            '/sh2/{a}-{b}',
            '/sh2/{x}-tail',
            '/sh3/static-word',
            '/sh3/{x}',
        ]
        app = self._app(asgi, routes)
        infos = inspect.inspect_route_reachability(app)
        by_template = self._by_template(infos)

        assert by_template['/sh/{a}.{b}'].status == 'reachable'
        shadowed = by_template['/sh/{x}.json']
        assert shadowed.status == 'unreachable'
        assert '/sh/{a}.{b}' in shadowed.reason
        assert shadowed.example_path is None
        assert shadowed.is_reachable is False

        assert by_template['/sh2/{a}-{b}'].status == 'reachable'
        shadowed2 = by_template['/sh2/{x}-tail']
        assert shadowed2.status == 'unreachable'
        assert '/sh2/{a}-{b}' in shadowed2.reason

        # The simple field sibling is not shadowed by the static segment,
        # and its example dodges the static sibling.
        simple = by_template['/sh3/{x}']
        assert simple.status == 'reachable'
        assert simple.example_path != '/sh3/static-word'

        self._assert_examples_route(app, infos)
        words = [
            '/sh/' + ''.join(chars)
            for length in range(1, 6)
            for chars in itertools.product('ab0.-~٣', repeat=length)
        ]
        self._assert_never_routes(app, '/sh/{x}.json', words)
        words2 = [
            '/sh2/' + ''.join(chars)
            for length in range(1, 7)
            for chars in itertools.product('ab0.-tail', repeat=length)
        ]
        self._assert_never_routes(app, '/sh2/{x}-tail', words2)

    def test_infeasible_int(self, asgi):
        routes = [
            '/bad1/{n:int(min=9, max=1)}',
            '/bad1/{n:int(min=9, max=1)}/kid',
            '/bad2/{n:int(num_digits=1, min=-1, max=-1)}',
            '/bad3/{n:int(num_digits=2, min=100, max=200)}.json',
        ]
        app = self._app(asgi, routes)
        infos = inspect.inspect_route_reachability(app)
        for info in infos:
            assert info.status == 'unreachable', info.uri_template
            assert info.example_path is None
            assert '"n"' in info.reason, info.reason
        self._assert_never_routes(
            app,
            '/bad1/{n:int(min=9, max=1)}',
            [
                '/bad1/' + ''.join(chars)
                for length in range(1, 5)
                for chars in itertools.product('0123456789+-_٠١', repeat=length)
            ],
        )

    def test_unknown_templates(self, asgi):
        routes = [
            '/u1/{id:uuid}',
            '/u2/caf\u00e9',
            '/u2/caf\u00e9/{x}',
            '/u3/{k:int(1+1)}',
            '/u4/{k:int(1+1)}.json',
            '/u5/{x}.caf\u00e9',
            '/u6/{x:float}.json',
            '/u7/{a:int}.caf\u00e9',
            '/u8/{x:uuid}.json',
            '/u8/{y}-{z}',
            '/u8/{w}',
            '/u9/{x:uuid}.json/deep',
            '/u9/{z}',
            '/u10/{a}.{b}',
            '/u10/{x:uuid}',
            '/u11/caf\u00e9-{x}',
        ]
        app = self._app(asgi, routes)
        infos = inspect.inspect_route_reachability(app)
        by_template = self._by_template(infos)

        assert by_template['/u1/{id:uuid}'].status == 'unknown'
        assert 'uuid' in by_template['/u1/{id:uuid}'].reason
        assert by_template['/u2/caf\u00e9'].status == 'unknown'
        assert by_template['/u2/caf\u00e9/{x}'].status == 'unknown'
        assert by_template['/u3/{k:int(1+1)}'].status == 'unknown'
        assert 'integer' in by_template['/u3/{k:int(1+1)}'].reason
        assert by_template['/u4/{k:int(1+1)}.json'].status == 'unknown'
        assert by_template['/u5/{x}.caf\u00e9'].status == 'unknown'
        assert by_template['/u6/{x:float}.json'].status == 'unknown'
        assert 'float' in by_template['/u6/{x:float}.json'].reason
        assert by_template['/u7/{a:int}.caf\u00e9'].status == 'unknown'

        # An unsupported complex sibling registered earlier may intercept
        # the later routes, so they must be reported as unknown.
        influenced = by_template['/u8/{y}-{z}']
        assert influenced.status == 'unknown'
        assert '/u8/{x:uuid}.json' in influenced.reason
        assert by_template['/u8/{w}'].status == 'unknown'

        # The reason for an intermediate node points at a descendant.
        intermediate = by_template['/u9/{z}']
        assert intermediate.status == 'unknown'
        assert '/u9/{x:uuid}.json/deep' in intermediate.reason

        # An unsupported sibling registered later cannot shadow anything.
        assert by_template['/u10/{a}.{b}'].status == 'reachable'

        # A non-ASCII literal preceding the field is also unsupported.
        assert by_template['/u11/caf\u00e9-{x}'].status == 'unknown'
        self._assert_examples_route(app, infos)

    def test_static_segment_shadows_int_digit(self, asgi):
        routes = ['/d/0', '/d/{n:int}']
        app = self._app(asgi, routes)
        infos = inspect.inspect_route_reachability(app)
        info = self._by_template(infos)['/d/{n:int}']
        assert info.status == 'reachable'
        # The example must dodge the static "0" sibling.
        assert info.example_path == '/d/1'
        self._assert_examples_route(app, infos)

    def test_int_shadowed_by_int_is_unknown(self, asgi):
        routes = ['/c/{n:int}.{b}', '/c/{a:int}.x']
        app = self._app(asgi, routes)
        infos = self._by_template(inspect.inspect_route_reachability(app))
        info = infos['/c/{a:int}.x']
        # Every word the target accepts is really consumed first, but the
        # static analysis of the int fall-through is conservatively unknown.
        assert info.status == 'unknown'
        assert info.example_path is None

    def test_solve_segment_falls_back_to_synthetic(self, asgi, monkeypatch):
        app = self._app(asgi, ['/i/{n:int(min=10)}'])
        node = app._router._roots[0].children[0]
        siblings = app._router._roots[0].children
        # Force readable enumeration to yield nothing; the Arabic-Indic
        # synthetic probe must still establish reachability.
        monkeypatch.setattr(inspect, '_ENUMERATION_EXPANSION_BUDGET', 0)
        status, word, reason = inspect._solve_segment(
            app._router,
            siblings,
            node,
            [('int', '')],
            {'n': (None, 10, None)},
        )
        assert status == 'reachable'
        assert reason is None
        assert inspect._int_convert(word, (None, 10, None)) == 10
        assert app._router._find.__name__ == '_compile_and_find'

    def test_synthetic_probe_after_truncated_readable_scan(self, asgi, monkeypatch):
        # The int value is pinned to 0; forcing the expansion budget to
        # zero skips readable candidates, while the all-Arabic-Indic
        # zero word survives the model-difference gate.
        app = self._app(asgi, ['/z/{n:int(min=0, max=0)}'])
        node = app._router._roots[0].children[0]
        siblings = app._router._roots[0].children
        monkeypatch.setattr(inspect, '_ENUMERATION_EXPANSION_BUDGET', 0)
        status, word, reason = inspect._solve_segment(
            app._router,
            siblings,
            node,
            [('int', '')],
            {'n': (None, 0, 0)},
        )
        assert status == 'reachable'
        assert reason is None
        assert word == '٠'
        assert inspect._int_convert(word, (None, 0, 0)) == 0
        assert app._router._find.__name__ == '_compile_and_find'

    def test_unicode_digit_example_dodges_static_routes(self, asgi):
        # Every length-4 spelling of 0 reachable within the bounded
        # readable enumeration is consumed by a static route first; the
        # all-Arabic-Indic spelling really routes to the field at runtime
        # and is found by the synthetic-word probe.
        blocked = [
            '/j/0000',
            '/j/000٠',
            '/j/00_0',
            '/j/00_٠',
            '/j/00٠0',
            '/j/00٠٠',
        ]
        template = '/j/{n:int(num_digits=4, min=0, max=0)}'
        app = self._app(asgi, blocked + [template])
        infos = inspect.inspect_route_reachability(app)
        assert app._router._find.__name__ == '_compile_and_find'
        info = self._by_template(infos)[template]
        assert info.status == 'reachable'
        assert info.example_path == '/j/٠٠٠٠'
        self._assert_examples_route(app, infos)
        self._assert_never_routes(app, template, blocked)

    def test_int_template_shadowed_by_plain_complex(self, asgi):
        # The plain two-field template has a different shape (so the
        # router accepts both), yet its ".+" language is a superset of
        # the int template's: every "...N.json" word is greedily
        # consumed as a="N", b="json".
        routes = ['/pl/{a}.{b}', '/pl/{n:int}.json']
        app = self._app(asgi, routes)
        shadowed = self._by_template(inspect.inspect_route_reachability(app))[
            '/pl/{n:int}.json'
        ]
        assert shadowed.status == 'unreachable'
        assert shadowed.example_path is None
        assert '/pl/{a}.{b}' in shadowed.reason
        # The plain complex sibling really swallows every digit word.
        words = [
            '/pl/' + ''.join(chars) + '.json'
            for length in range(1, 5)
            for chars in itertools.product('0123456789٠', repeat=length)
        ]
        self._assert_never_routes(app, '/pl/{n:int}.json', words)

    def test_synthetic_scan_skips_blocked_word(self, asgi, monkeypatch):
        # The first Arabic-Indic candidate equals a static segment and
        # is rejected; the probe must continue to the next candidate.
        routes = [
            '/w/٠.٠',
            '/w/{n:int(min=0, max=0)}.{m:int}',
        ]
        app = self._app(asgi, routes)
        monkeypatch.setattr(inspect, '_ENUMERATION_EXPANSION_BUDGET', 0)
        infos = inspect.inspect_route_reachability(app)
        assert app._router._find.__name__ == '_compile_and_find'
        info = self._by_template(infos)['/w/{n:int(min=0, max=0)}.{m:int}']
        assert info.status == 'reachable'
        assert info.example_path == '/w/٠.١'
        assert app._router.find('/w/٠.١')[3] == info.uri_template
        assert app._router.find('/w/٠.٠')[3] == '/w/٠.٠'

    def test_path_converter(self, asgi):
        routes = ['/p1/{rest:path}', '/p2/{a}.{b}', '/p2/{rest:path}']
        app = self._app(asgi, routes)
        infos = inspect.inspect_route_reachability(app)
        by_template = self._by_template(infos)
        assert by_template['/p1/{rest:path}'].example_path == '/p1/a'
        path_info = by_template['/p2/{rest:path}']
        assert path_info.status == 'reachable'
        self._assert_examples_route(app, infos)

    def test_overridden_int_converter_is_unknown(self, asgi):
        class ExplodingInt(routing.converters.IntConverter):
            def convert(self, value):
                raise AssertionError('user code must not be executed')

        app = get_app(asgi, cors=False)
        app.router_options.converters['int'] = ExplodingInt
        resource = i_f.MyResponderAsync() if asgi else i_f.MyResponder()
        app.add_route('/x/{n:int(1)}', resource)
        app.add_route('/y/{n:int(1)}.json', resource)

        infos = inspect.inspect_route_reachability(app)
        assert app._router._find.__name__ == '_compile_and_find'
        assert {info.status for info in infos} == {'unknown'}

    def test_unsupported_router_raises(self):
        app = get_app(False, cors=False)
        app._router = object()
        with pytest.raises(TypeError, match='default CompiledRouter'):
            inspect.inspect_route_reachability(app)

    def test_string_visitor(self, asgi):
        routes = ['/v/{a}.{b}', '/v/{x}.json', '/v/{id:uuid}']
        app = self._app(asgi, routes)
        infos = self._by_template(inspect.inspect_route_reachability(app))

        text = infos['/v/{a}.{b}'].to_string()
        assert text == str(infos['/v/{a}.{b}'])
        assert text == '⇒ /v/{a}.{b} → reachable (e.g. /v/a.a)'

        text = infos['/v/{x}.json'].to_string()
        assert text.startswith('⇒ /v/{x}.json → unreachable: ')
        assert '/v/{a}.{b}' in text

        text = infos['/v/{id:uuid}'].to_string()
        assert '→ unknown:' in text

    def test_status_constants(self):
        assert inspect.RouteReachabilityInfo.REACHABLE == 'reachable'
        assert inspect.RouteReachabilityInfo.UNREACHABLE == 'unreachable'
        assert inspect.RouteReachabilityInfo.UNKNOWN == 'unknown'


@pytest.mark.parametrize(
    'argstr, expected',
    [
        (None, (None, None, None)),
        ('', (None, None, None)),
        ('3', (3, None, None)),
        ('3, 1, 9', (3, 1, 9)),
        ('min=1, max=9, num_digits=3', (3, 1, 9)),
        ('None', (None, None, None)),
        ('None, None, None', (None, None, None)),
        ('-2', (-2, None, None)),
        ('min=-5, max=-1', (None, -5, -1)),
        ('num_digits=2, max=99', (2, None, 99)),
    ],
)
def test_parse_int_args_valid(argstr, expected):
    assert inspect._parse_int_args(argstr) == expected


@pytest.mark.parametrize(
    'argstr',
    [
        '1+1',
        'x',
        '*[1]',
        '**{}',
        'lambda: 1',
        'True',
        '1, 2, 3, 4',
        'num_digits=1, num_digits=2',
        'unknown=1',
        '1, foo=2',
        '\x00',
        'min=',
        '-x',
        '1).x',
        '1).x or (',
        '1).x(',
        'num_digits=1+1',
    ],
)
def test_parse_int_args_invalid(argstr):
    assert inspect._parse_int_args(argstr) is None


@pytest.mark.parametrize(
    'args, feasible',
    [
        ((None, None, None), True),
        ((None, 10, 99), True),
        ((None, None, -1), True),
        ((None, None, 0), True),
        ((None, -5, -1), True),
        ((None, 5, 1), False),
        ((None, 1, -1), False),
        ((1, -1, -1), False),
        ((2, -1, -1), True),
        ((3, 100, 999), True),
        ((2, 100, 200), False),
        ((0, None, None), False),
    ],
)
def test_int_example_value(args, feasible):
    value = inspect._int_example_value(args)
    assert (value is not None) == feasible
    if feasible:
        word = inspect._unicode_int_word(value, args[0])
        assert word is not None
        assert inspect._int_convert(word, args) is not None


def test_int_convert_mirrors_runtime():
    assert inspect._int_convert('12', (None, None, None)) == 12
    assert inspect._int_convert('١٢', (None, None, None)) == 12
    assert inspect._int_convert('-1', (None, -5, -1)) == -1
    assert inspect._int_convert('12', (3, None, None)) is None
    assert inspect._int_convert(' 1', (None, None, None)) is None
    assert inspect._int_convert('+', (None, None, None)) is None
    assert inspect._int_convert('9', (None, 0, 5)) is None
    assert inspect._int_convert('-1', (None, 0, 5)) is None


def test_unicode_int_word_too_wide():
    assert inspect._unicode_int_word(123, 2) is None
    assert inspect._unicode_int_word(0, 3) == '٠٠٠'


def test_iter_difference_words_bounds(monkeypatch):
    symbols, _ = inspect._build_symbols([('plain', '')], [[('lit', 'a')]])
    target = inspect._determinize(inspect._build_nfa([[('plain', '')]]), symbols)
    earlier = inspect._determinize(inspect._build_nfa([[('lit', 'a')]]), symbols)

    words = list(inspect._iter_difference_words(target, earlier, symbols, 1, 3))
    assert ('a',) not in words
    assert words
    assert words[0] == ('b',)

    # An earlier language that accepts only the empty word behaves as dead
    # for non-empty words; the shortest filler symbol then wins.
    plain_symbols, _ = inspect._build_symbols([('plain', '')], [])
    plain_target = inspect._determinize(
        inspect._build_nfa([[('plain', '')]]), plain_symbols
    )
    dead = inspect._determinize(inspect._build_nfa([[]]), plain_symbols)
    words = list(
        inspect._iter_difference_words(plain_target, dead, plain_symbols, 1, 1)
    )
    assert words[0][0] == 'a'

    monkeypatch.setattr(inspect, '_ENUMERATION_WORD_LIMIT', 2)
    words = list(
        inspect._iter_difference_words(plain_target, dead, plain_symbols, 1, 6)
    )
    assert len(words) == 2

    monkeypatch.setattr(inspect, '_ENUMERATION_WORD_LIMIT', 1000)
    monkeypatch.setattr(inspect, '_ENUMERATION_EXPANSION_BUDGET', 3)
    words = list(
        inspect._iter_difference_words(plain_target, dead, plain_symbols, 1, 6)
    )
    assert len(words) <= 1000


def test_is_model_difference():
    symbols, representative = inspect._build_symbols([('plain', '')], [[('lit', 'a')]])
    target = inspect._determinize(inspect._build_nfa([[('plain', '')]]), symbols)
    earlier = inspect._determinize(inspect._build_nfa([[('lit', 'a')]]), symbols)

    assert inspect._is_model_difference('b', representative, target, earlier)
    assert not inspect._is_model_difference('a', representative, target, earlier)
    # Characters outside the partition alphabet cannot be classified.
    assert not inspect._is_model_difference('#', representative, target, earlier)
    # A word longer than the literal-only earlier language still differs.
    assert inspect._is_model_difference('aa', representative, target, earlier)
    literal_target = inspect._determinize(inspect._build_nfa([[('lit', 'a')]]), symbols)
    assert not inspect._is_model_difference(
        'b', representative, literal_target, earlier
    )


def test_build_symbols_partition():
    # Plain-only alphabet: filler letters and digits, no int extras.
    symbols, representative = inspect._build_symbols([('plain', '')], [])
    assert symbols[:2] == ['a', 'b']
    assert '+' not in symbols
    assert inspect._UNICODE_DIGIT_SYM not in symbols
    assert representative['a'] == 'a'

    # A literal consumes its character; the first filler moves on.
    symbols, _ = inspect._build_symbols([('lit', 'a'), ('plain', '')], [])
    assert symbols[0] == 'b'
    assert 'a' in symbols

    # Int tokens add the sign/underscore characters and the Unicode
    # sentinel; a '-' already present as a literal is not duplicated.
    symbols, representative = inspect._build_symbols([('lit', '-'), ('int', '')], [])
    assert '-' in symbols
    assert symbols.count('-') == 1
    assert {'+', '_', inspect._UNICODE_DIGIT_SYM} <= set(symbols)
    assert representative[inspect._UNICODE_DIGIT_SYM] == inspect._UNICODE_DIGITS[0]
    assert inspect._label_matches('any', '+') is True
    assert inspect._label_matches('intchar', 'a') is False
    assert inspect._label_matches('intchar', inspect._UNICODE_DIGIT_SYM) is True
    assert inspect._label_matches(('lit', '-'), '-') is True
    assert inspect._label_matches(('lit', '-'), '+') is False


def test_node_template_fallbacks():
    assert (
        inspect._node_template(
            types.SimpleNamespace(uri_template=None, children=[], raw_segment='seg')
        )
        == 'seg'
    )


def test_true_accept_complex_non_matching():
    app = falcon.App()

    class Resource:
        def on_get(self, req, resp):
            pass

    app.add_route('/t/{n:int}.json', Resource())
    node = app._router._roots[0].children[0]
    assert inspect._true_accept(node, [], {}, 'anything') is False
    assert inspect._true_accept(node, [('lit', 'x')], {}, 'x') is False
    # The complex regex matches, but the int conversion rejects the group.
    assert inspect._true_accept(node, [], {'n': (None, None, None)}, 'a.json') is False
    assert inspect._true_accept(node, [], {'n': (None, None, None)}, '1.json') is True
    assert inspect._true_accept(node, [], {'n': (None, 10, None)}, '1.json') is False


def test_int_candidate_values():
    assert inspect._int_candidate_values((None, None, None)) == [0, 1]
    assert inspect._int_candidate_values((None, 10, 99)) == [10, 11]
    assert inspect._int_candidate_values((None, None, -1)) == [-1]
    assert inspect._int_candidate_values((1, 9, 9)) == [9]
    # Infeasible arguments yield no candidates (the caller rejects the
    # template before reaching synthetic probing).
    assert inspect._int_candidate_values((1, 10, 99)) == []
    # num_digits below 1 cannot be registered (the converter raises), but
    # the rendering guard remains defensive against direct calls.
    assert inspect._int_candidate_values((0, None, None)) == []


def test_synthetic_words():
    symbols, representative = inspect._build_symbols(
        [('plain', ''), ('lit', '.'), ('int', '')], []
    )
    plain_char = next(iter(representative))
    words = inspect._synthetic_words(
        [('plain', ''), ('lit', '.'), ('int', '')],
        {'n': (None, 10, 99)},
        representative,
        plain_char,
    )
    assert words == [plain_char + '.١٠', plain_char + '.١١']
    words = inspect._synthetic_words([('lit', 'x')], {}, representative, plain_char)
    assert words == ['x']
    # Defensive fallback for an int field without feasible candidates.
    words = inspect._synthetic_words(
        [('int', '')], {'n': (1, 10, 99)}, representative, plain_char
    )
    assert words == [inspect._UNICODE_DIGITS[0]]


def test_shadow_reason_without_models():
    assert inspect._shadow_reason([]) == (
        'The route template is shadowed by an earlier sibling route.'
    )


def test_node_template_bfs():
    grandchild = types.SimpleNamespace(uri_template='/a/{x}/deep', children=[])
    child = types.SimpleNamespace(uri_template=None, children=[grandchild])
    root = types.SimpleNamespace(uri_template=None, children=[child])
    assert inspect._node_template(root) == '/a/{x}/deep'


def test_verify_word_earlier_gate():
    # Defense-in-depth: even if an earlier route's modeled tokens wrongly
    # rejected the word, its real compiled regex must reject it too.
    app = falcon.App()

    class Resource:
        def on_get(self, req, resp):
            pass

    app.add_route('/e/{x}', Resource())
    earlier = app._router._roots[0].children[0]
    app.add_route('/e/t', Resource())
    target = app._router._roots[0].children[1]

    # The earlier simple field's real matcher accepts any nonempty word.
    assert (
        inspect._verify_word(
            'a',
            target,
            [('lit', 't')],
            {},
            [(earlier, [('plain', '')], {})],
            {'t'},
        )
        is None
    )
    # Static exclusion still applies.
    assert inspect._verify_word('t', target, [('lit', 't')], {}, [], {'t'}) is None
    assert inspect._verify_word('t', target, [('lit', 't')], {}, [], set()) == 't'
