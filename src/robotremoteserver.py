#  Copyright 2008-2015 Nokia Networks
#  Copyright 2016- Robot Framework Foundation
#  Copyright Brolsorro
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

from __future__ import print_function

import inspect
import os
import re
import select
import signal
import sys
import threading
import traceback
import logging
import socket

# import queue
# import psutil
# import multiprocessing as mp
# import threading as th


from typing import Dict, List, Union, NamedTuple, Any
from io import StringIO
from concurrent.futures import ThreadPoolExecutor
from xmlrpc.client import Binary, ServerProxy
from xmlrpc.server import (
    MultiPathXMLRPCServer,
    SimpleXMLRPCDispatcher,
    SimpleXMLRPCServer,
)
from inspect import getfullargspec
from xmlrpc.server import SimpleXMLRPCRequestHandler
from collections.abc import Mapping
from urllib.parse import urlencode, urlunparse
from robot.errors import RemoteError


PY2, PY3 = False, True
unicode = str
long = int
logging.basicConfig(level=logging.INFO)

# if sys.version_info < (3,):
#     from SimpleXMLRPCServer import SimpleXMLRPCServer
#     from StringIO import StringIO
#     from xmlrpclib import Binary, ServerProxy
#     from collections import Mapping
#     PY2, PY3 = True, False
#     def getfullargspec(func):
#         return inspect.getargspec(func) + ([], None, {})
# else:
# from inspect import getfullargspec
# from io import StringIO
# from xmlrpc.client import Binary, ServerProxy
# from xmlrpc.server import SimpleXMLRPCServer
# from collections.abc import Mapping
# PY2, PY3 = False, True
# unicode = str
# long = int


__all__ = [
    "RobotRemoteMultiServer",
    "RobotRemoteServer",
    "stop_remote_server",
    "set_remote_global_timeout",
    "test_remote_server",
]
__version__ = "1.2.1"

BINARY = re.compile("[\x00-\x08\x0B\x0C\x0E-\x1F]")
NON_ASCII = re.compile("[\x80-\xff]")
# GLOBAL_KEYWORDS_TIMEOUT = 1800

# QUEUE_TASKS = queue.Queue()
# LOCK = th.Lock()


# TODO: https://python.hotexamples.com/examples/SimpleXMLRPCServer/MultiPathXMLRPCServer/-/python-multipathxmlrpcserver-class-examples.html


# Оптимизация потоков через пул
class ThreadPoolMixIn:
    """Использует пул потоков для обработки запросов."""
    _executor = ThreadPoolExecutor(max_workers=20)

    def process_request(self, request, client_address):
        self._executor.submit(self.process_request_thread, request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)


class RequestHandler(SimpleXMLRPCRequestHandler):
    """Override Request Handler

    Args:
        SimpleXMLRPCRequestHandler (_type_): _description_
    """
    encode_threshold = 1400  # Сжимать ответы больше MTU

    rpc_paths = ("/", "/RPC", "/RPC2")
    def setup(self):
        super().setup()
        # Отключение алгоритма Нагла для мгновенной отправки пакетов
        self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

# class MultiPathThreadedXMLRPCServer(ThreadingMixIn, MultiPathXMLRPCServer): ...
class MultiPathThreadedXMLRPCServer(ThreadPoolMixIn, MultiPathXMLRPCServer): ...


# namedtuple to match the internal signature of urlunparse
class Components(NamedTuple):
    scheme: Any
    netloc: Any
    url: Any
    path: Any
    query: Any
    fragment: Any




class RobotRemoteMultiServer(object):
    """Entrypoint for creating remote XML-RPC connection

    Args:
        object (_type_): object

    Returns:
        _type_: instance class
    """

    MAIN_ROUTES = "main_routes"

    def __init__(
        self,
        *libraries: List[object],
        host="127.0.0.1",
        port=8270,
        port_file=None,
        allow_stop="DEPRECATED",
        serve=True,
        allow_remote_stop=True,
        debug_mode=False,
    ):
        """Configure and start-up remote server.

        :param library:     Test library instance or module to host.
        :param host:        Address to listen. Use ``'0.0.0.0'`` to listen
                            to all available interfaces.
        :param port:        Port to listen. Use ``0`` to select a free port
                            automatically. Can be given as an integer or as
                            a string.
        :param port_file:   File to write the port that is used. ``None`` means
                            no such file is written. Port file is created after
                            the server is started and removed automatically
                            after it has stopped.
        :param allow_stop:  DEPRECATED since version 1.1. Use
                            ``allow_remote_stop`` instead.
        :param serve:       If ``True``, start the server automatically and
                            wait for it to be stopped.
        :param allow_remote_stop:  Allow/disallow stopping the server using
                            ``Stop Remote Server`` keyword and
                            ``stop_remote_server`` XML-RPC method.
        """
        # self._library = RemoteLibraryFactory(library)
        self._libraries: Dict[str, RemoteLibraryFactory] = self._get_route_names(
            libraries
        )
        self.log = logging.getLogger("remote-multi-server")
        reqhand = RequestHandler
        reqhand.rpc_paths = reqhand.rpc_paths + tuple(
            [f"/{v}" for v in self._libraries.keys()]
        )
        self._server = StoppableMultiXMLRPCServer(host, int(port), reqhand, debug_mode)
        self._port_file = port_file
        self._allow_remote_stop = (
            allow_remote_stop if allow_stop == "DEPRECATED" else allow_stop
        )

        self._add_dispachers()

        if serve:
            self.serve()

    def _get_route_names(self, libs):
        """Get paths portions the URL for receiving XML-RPC requests.

        Args:
            libs (_type_): _description_

        Returns:
            _type_: _description_
        """
        route_objs = dict()
        route = None
        for index, v in enumerate(libs):

            if hasattr(v, "REMOTE_LIBRARY_ROUTE") and isinstance(
                v.REMOTE_LIBRARY_ROUTE, str
            ):
                route = v.REMOTE_LIBRARY_ROUTE
            else:
                route = type(v).__name__

            route_objs[route] = {"_class": RemoteLibraryFactory(v)}
            if index == 0:
                route_objs[route][self.MAIN_ROUTES] = RequestHandler.rpc_paths
        # a = {   :v for v in libs }
        logging.debug(f"{route_objs}")
        return route_objs

    def _add_dispachers(self):
        """Create some dispachers for paths portions of the URL"""

        self.log.debug("init dispachers")
        paths_portions = dict()
        #
        for path in self._libraries:

            class tmp(RobotRemoteServer):
                def __init__(self, library, server, allow_remote_stop):
                    self._library = library
                    self._allow_remote_stop = allow_remote_stop
                    self._server = server

            rrt = tmp(
                self._libraries[path]["_class"], self._server, self._allow_remote_stop
            )

            dispatch = SimpleXMLRPCDispatcher(allow_none=True, encoding="utf-8")
            if (
                self.MAIN_ROUTES in self._libraries[path]
                and self._libraries[path][self.MAIN_ROUTES]
            ):
                for route in self._libraries[path][self.MAIN_ROUTES]:
                    self._server.add_dispatcher(route, dispatch)
            self._register_functions(rrt, dispatch)
            path_partion = f"/{path}"
            paths_portions[path_partion] = rrt._library
            self._server.add_dispatcher(path_partion, dispatch)
            self.log.info(f"'{path_partion}' dispatcher was registered!")

        self.log.info("Available URLs with own path portions")
        for path in self._libraries:
            class_name = type(self._libraries[path]["_class"]._library).__name__
            self.log.info(f"Addresses Remote Library for a <{class_name}> class:")
            additional_paths = self._libraries[path].get("main_routes", [])
            main_path = f"/{path}"
            for index, _path in enumerate([main_path, *additional_paths]):
                url = urlunparse(
                    Components(
                        scheme="http",
                        netloc=f"{self._server.server_address[0]}:{self._server.server_address[1]}",
                        query=urlencode([]),
                        path="",
                        url=_path,
                        fragment="",
                    )
                )
                if index == 0:
                    self.log.info(f"main_address:{url}")
                else:
                    self.log.info(f"additional_address:{url}")

    def _set_path(self, path):
        """Set runtime path"""
        self.path = path

    @staticmethod
    def _register_functions(obj, server):
        """Register a functions that can respond to XML-RPC requests
        and call to robotframework commands.

        Args:
            obj (_type_): Entrypoint for creating remote XML-RPC connection
            server (_type_): XML-RPC server
        """
        server.register_function(obj.get_keyword_names)
        server.register_function(obj.run_keyword)
        server.register_function(obj.get_keyword_tags)
        server.register_function(obj.get_keyword_arguments)
        server.register_function(obj.get_keyword_documentation)
        server.register_function(obj.get_keyword_types)
        server.register_function(obj.stop_remote_server)
        server.register_function(obj.get_library_information)
        server.register_function(obj.set_remote_global_timeout)

    @property
    def server_address(self):
        """Server address as a tuple ``(host, port)``."""
        return self._server.server_address

    @property
    def server_port(self):
        """Server port as an integer.

        If the initial given port is 0, also this property returns 0 until
        the server is activated.
        """
        return self._server.server_address[1]

    def activate(self):
        """Bind port and activate the server but do not yet start serving.

        :return  Port number that the server is going to use. This is the
                 actual port to use, even if the initially given port is 0.
        """
        return self._server.activate()

    def serve(self, log=True):
        """Start the server and wait for it to be stopped.

        :param log:  When ``True``, print messages about start and stop to
                     the console.

        Automatically activates the server if it is not activated already.

        If this method is executed in the main thread, automatically registers
        signals SIGINT, SIGTERM and SIGHUP to stop the server.

        Using this method requires using ``serve=False`` when initializing the
        server. Using ``serve=True`` is equal to first using ``serve=False``
        and then calling this method.

        In addition to signals, the server can be stopped with the ``Stop
        Remote Server`` keyword and the ``stop_remote_serve`` XML-RPC method,
        unless they are disabled when the server is initialized. If this method
        is executed in a thread, then it is also possible to stop the server
        using the :meth:`stop` method.
        """
        self._server.activate()
        self._announce_start(log, self._port_file)
        with SignalHandler(self.stop):
            self._server.serve()
        self._announce_stop(log, self._port_file)

    def _announce_start(self, log, port_file):
        self._log("started", log)
        if port_file:
            with open(port_file, "w") as pf:
                pf.write(str(self.server_port))

    def _announce_stop(self, log, port_file):
        self._log("stopped", log)
        if port_file and os.path.exists(port_file):
            os.remove(port_file)

    def _log(self, action, log=True, warn=False):
        if log:
            address = "%s:%s" % self.server_address
            if warn:
                print("*WARN*", end=" ")
            self.log.info("Robot Framework remote server at %s %s." % (address, action))

    def stop(self):
        """Stop server."""
        self._server.stop()

    # Exposed XML-RPC methods. Should they be moved to own class?

    def stop_remote_server(self, log=True):
        if not self._allow_remote_stop:
            self._log("does not allow stopping", log, warn=True)
            return False
        self.stop()
        return True


class StoppableMultiXMLRPCServer(MultiPathThreadedXMLRPCServer):
    allow_reuse_address = True

    def __init__(self, host, port, reqhand, debug_mode=False):
        MultiPathThreadedXMLRPCServer.__init__(
            self,
            (host, port),
            logRequests=debug_mode,
            bind_and_activate=False,
            requestHandler=reqhand,
        )
        self._activated = False
        self._stopper_thread = None
        self.log = logging.getLogger("net")

    def activate(self):
        if not self._activated:
            self.server_bind()
            self.server_activate()
            self._activated = True
        self.log.info(f"port_bind:{self.server_address[1]}")
        return self.server_address[1]

    def serve(self):
        self.activate()
        try:
            self.serve_forever()
        except select.error:
            # Signals seem to cause this error with Python 2.6.
            if sys.version_info[:2] > (2, 6):
                raise
        self.server_close()
        if self._stopper_thread:
            self._stopper_thread.join()
            self._stopper_thread = None

    def stop(self):
        self._stopper_thread = threading.Thread(target=self.shutdown)
        self._stopper_thread.daemon = True
        self._stopper_thread.start()


class RobotRemoteServer(object):

    def __init__(
        self,
        library,
        host="127.0.0.1",
        port=8270,
        port_file=None,
        allow_stop="DEPRECATED",
        serve=True,
        allow_remote_stop=True,
    ):
        """Configure and start-up remote server.

        :param library:     Test library instance or module to host.
        :param host:        Address to listen. Use ``'0.0.0.0'`` to listen
                            to all available interfaces.
        :param port:        Port to listen. Use ``0`` to select a free port
                            automatically. Can be given as an integer or as
                            a string.
        :param port_file:   File to write the port that is used. ``None`` means
                            no such file is written. Port file is created after
                            the server is started and removed automatically
                            after it has stopped.
        :param allow_stop:  DEPRECATED since version 1.1. Use
                            ``allow_remote_stop`` instead.
        :param serve:       If ``True``, start the server automatically and
                            wait for it to be stopped.
        :param allow_remote_stop:  Allow/disallow stopping the server using
                            ``Stop Remote Server`` keyword and
                            ``stop_remote_server`` XML-RPC method.
        """
        self._library = RemoteLibraryFactory(library)
        self._server = StoppableXMLRPCServer(host, int(port))
        self._register_functions(self._server)
        self._port_file = port_file
        self._allow_remote_stop = (
            allow_remote_stop if allow_stop == "DEPRECATED" else allow_stop
        )
        if serve:
            self.serve()

    def _register_functions(self, server):
        server.register_function(self.get_keyword_names)
        server.register_function(self.run_keyword)
        server.register_function(self.get_keyword_tags)
        server.register_function(self.get_keyword_arguments)
        server.register_function(self.get_keyword_documentation)
        server.register_function(self.get_keyword_types)
        server.register_function(self.stop_remote_server)
        server.register_function(self.get_library_information)
        server.register_function(self.set_remote_global_timeout)

    @property
    def server_address(self):
        """Server address as a tuple ``(host, port)``."""
        return self._server.server_address

    @property
    def server_port(self):
        """Server port as an integer.

        If the initial given port is 0, also this property returns 0 until
        the server is activated.
        """
        return self._server.server_address[1]

    def activate(self):
        """Bind port and activate the server but do not yet start serving.

        :return  Port number that the server is going to use. This is the
                 actual port to use, even if the initially given port is 0.
        """
        return self._server.activate()

    def serve(self, log=True):
        """Start the server and wait for it to be stopped.

        :param log:  When ``True``, print messages about start and stop to
                     the console.

        Automatically activates the server if it is not activated already.

        If this method is executed in the main thread, automatically registers
        signals SIGINT, SIGTERM and SIGHUP to stop the server.

        Using this method requires using ``serve=False`` when initializing the
        server. Using ``serve=True`` is equal to first using ``serve=False``
        and then calling this method.

        In addition to signals, the server can be stopped with the ``Stop
        Remote Server`` keyword and the ``stop_remote_serve`` XML-RPC method,
        unless they are disabled when the server is initialized. If this method
        is executed in a thread, then it is also possible to stop the server
        using the :meth:`stop` method.
        """
        self._server.activate()
        self._announce_start(log, self._port_file)
        with SignalHandler(self.stop):
            self._server.serve()
        self._announce_stop(log, self._port_file)

    def _announce_start(self, log, port_file):
        self._log("started", log)
        if port_file:
            with open(port_file, "w") as pf:
                pf.write(str(self.server_port))

    def _announce_stop(self, log, port_file):
        self._log("stopped", log)
        if port_file and os.path.exists(port_file):
            os.remove(port_file)

    def _log(self, action, log=True, warn=False):
        if log:
            address = "%s:%s" % self.server_address
            if warn:
                print("*WARN*", end=" ")
            print("Robot Framework remote server at %s %s." % (address, action))

    def stop(self):
        """Stop server."""
        self._server.stop()

    # Exposed XML-RPC methods. Should they be moved to own class?

    def set_remote_global_timeout(self, timeout: Union[int, float]) -> bool:
        """Set Remote Global Timeout

        Args:
            timeout (int): Default: 3600
        """
        
        raise NotImplementedError("Ключевое слово не работает! Не пытайтесь его выполнять!")
        
        # global GLOBAL_KEYWORDS_TIMEOUT

        # if isinstance(timeout, str):
        #     try:
        #         if timeout.isdigit():
        #             timeout = int(timeout)
        #         elif timeout.count(".") == 1:
        #             timeout = float(timeout)
        #         else:
        #             timeout = float(timeout)

        #     except Exception as e:
        #         logging.info(e)
        #         RemoteError(
        #             "Invalid type specified. Timeout can only have a numeric value"
        #         )

        # if GLOBAL_KEYWORDS_TIMEOUT != timeout:
        #     GLOBAL_KEYWORDS_TIMEOUT = timeout
        #     print(
        #         f"A global keyword interrupt timeout has been set for the agent: {GLOBAL_KEYWORDS_TIMEOUT} seconds"
        #     )
        # else:
        #     print(
        #         f"Timeout was already set: {GLOBAL_KEYWORDS_TIMEOUT}. Nothing changed"
        #     )
        # return True

    def stop_remote_server(self, log=True):
        if not self._allow_remote_stop:
            self._log("does not allow stopping", log, warn=True)
            return False
        self.stop()
        return True

    def get_keyword_names(self):
        return self._library.get_keyword_names() + [
            "stop_remote_server",
            "set_remote_global_timeout",
        ]

    def run_keyword(self, name, args, kwargs=None):
        if name == "stop_remote_server":
            return KeywordRunner(self.stop_remote_server).run_keyword(args, kwargs)
        if name == "set_remote_global_timeout":
            return KeywordRunner(self.set_remote_global_timeout).run_keyword(
                args, kwargs
            )
        return self._library.run_keyword(
            name,
            args,
            kwargs,
        )

    def get_library_information(self):
        return self._library.get_library_information()

    def get_keyword_arguments(self, name):
        if name == "stop_remote_server":
            return []
        if name == "set_remote_global_timeout":
            return ["timeout"]
        return self._library.get_keyword_arguments(name)

    def get_keyword_documentation(self, name):
        if name == "stop_remote_server":
            return (
                "Stop the remote server unless stopping is disabled.\n\n"
                "Return ``True/False`` depending was server stopped or not."
            )
        if name == "set_remote_global_timeout":
            a = "\n".join(
                filter(
                    None,
                    (
                        s.strip() + "\n"
                        for s in self.set_remote_global_timeout.__doc__.split("\n")
                    ),
                )
            )

            return a
        return self._library.get_keyword_documentation(name)

    def get_keyword_tags(self, name):
        if name in ["stop_remote_server", "set_remote_global_timeout"]:
            return []
        return self._library.get_keyword_tags(name)

    def get_keyword_types(self, name):
        if name in ["stop_remote_server", "set_remote_global_timeout"]:
            return None
        return self._library.get_keyword_types(name)


class StoppableXMLRPCServer(SimpleXMLRPCServer):
    allow_reuse_address = True

    def __init__(self, host, port):
        SimpleXMLRPCServer.__init__(
            self, (host, port), logRequests=False, bind_and_activate=False
        )
        self._activated = False
        self._stopper_thread = None

    def activate(self):
        if not self._activated:
            self.server_bind()
            self.server_activate()
            self._activated = True
        return self.server_address[1]

    def serve(self):
        self.activate()
        try:
            self.serve_forever()
        except select.error:
            # Signals seem to cause this error with Python 2.6.
            if sys.version_info[:2] > (2, 6):
                raise
        self.server_close()
        if self._stopper_thread:
            self._stopper_thread.join()
            self._stopper_thread = None

    def stop(self):
        self._stopper_thread = threading.Thread(target=self.shutdown)
        self._stopper_thread.daemon = True
        self._stopper_thread.start()


class SignalHandler(object):

    def __init__(self, handler):
        self._handler = lambda signum, frame: handler()
        self._original = {}

    def __enter__(self):
        for name in "SIGINT", "SIGTERM", "SIGHUP":
            if hasattr(signal, name):
                try:
                    orig = signal.signal(getattr(signal, name), self._handler)
                except ValueError:  # Not in main thread
                    return
                self._original[name] = orig

    def __exit__(self, *exc_info):
        while self._original:
            name, handler = self._original.popitem()
            signal.signal(getattr(signal, name), handler)


def RemoteLibraryFactory(library):
    if inspect.ismodule(library):
        return StaticRemoteLibrary(library)
    get_keyword_names = dynamic_method(library, "get_keyword_names")
    if not get_keyword_names:
        return StaticRemoteLibrary(library)
    run_keyword = dynamic_method(library, "run_keyword")
    if not run_keyword:
        return HybridRemoteLibrary(library, get_keyword_names)
    return DynamicRemoteLibrary(library, get_keyword_names, run_keyword)


def dynamic_method(library, underscore_name):
    tokens = underscore_name.split("_")
    camelcase_name = tokens[0] + "".join(t.title() for t in tokens[1:])
    for name in underscore_name, camelcase_name:
        method = getattr(library, name, None)
        if method and is_function_or_method(method):
            return method
    return None


def is_function_or_method(item):
    return inspect.isfunction(item) or inspect.ismethod(item)


class StaticRemoteLibrary(object):

    def __init__(self, library):
        self._library = library
        self._names, self._robot_name_index = self._get_keyword_names(library)

    def _get_keyword_names(self, library):
        names = []
        robot_name_index = {}
        for name, kw in inspect.getmembers(library):
            if is_function_or_method(kw):
                if getattr(kw, "robot_name", None):
                    names.append(kw.robot_name)
                    robot_name_index[kw.robot_name] = name
                elif name[0] != "_":
                    names.append(name)
        return names, robot_name_index


    def get_library_information(self):
        """Возвращает всю информацию о библиотеке за один запрос."""
        names = self.get_keyword_names() + ["__intro__", "__init__"]
        result = {}
        
        
        for name in names:
            result[name] = {
                'args': self.get_keyword_arguments(name),
                'doc': self.get_keyword_documentation(name),
                'tags': self.get_keyword_tags(name),
                'types': self.get_keyword_types(name)
            }
        return result

    def get_keyword_names(self):
        return self._names

    def run_keyword(self, name, args, kwargs=None):
        kw = self._get_keyword(name)
        return KeywordRunner(kw).run_keyword(args, kwargs)

    def _get_keyword(self, name):
        if name in self._robot_name_index:
            name = self._robot_name_index[name]
        return getattr(self._library, name)

    def get_keyword_arguments(self, name):
        if __name__ == "__init__":
            return []
        kw = self._get_keyword(name)
        args, varargs, kwargs, defaults, _, _, _ = getfullargspec(kw)
        if inspect.ismethod(kw):
            args = args[1:]  # drop 'self'
        if defaults:
            args, names = args[: -len(defaults)], args[-len(defaults) :]
            args += ["%s=%s" % (n, d) for n, d in zip(names, defaults)]
        if varargs:
            args.append("*%s" % varargs)
        if kwargs:
            args.append("**%s" % kwargs)
        return args

    def get_keyword_documentation(self, name):
        if name == "__intro__":
            source = self._library
        elif name == "__init__":
            source = self._get_init(self._library)
        else:
            source = self._get_keyword(name)
        return inspect.getdoc(source) or ""

    def _get_init(self, library):
        if inspect.ismodule(library):
            return None
        init = getattr(library, "__init__", None)
        return init if self._is_valid_init(init) else None

    def _is_valid_init(self, init):
        if not init:
            return False
        # https://bitbucket.org/pypy/pypy/issues/2462/
        if "PyPy" in sys.version:
            if PY2:
                return init.__func__ is not object.__init__.__func__
            return init is not object.__init__
        return is_function_or_method(init)

    def get_keyword_tags(self, name):
        keyword = self._get_keyword(name)
        return getattr(keyword, "robot_tags", [])

    def get_keyword_types(self, name):
        keyword = self._get_keyword(name)
        return getattr(keyword, "robot_types", None)


class HybridRemoteLibrary(StaticRemoteLibrary):

    def __init__(self, library, get_keyword_names):
        StaticRemoteLibrary.__init__(self, library)
        self.get_keyword_names = get_keyword_names


class DynamicRemoteLibrary(HybridRemoteLibrary):

    def __init__(self, library, get_keyword_names, run_keyword):
        HybridRemoteLibrary.__init__(self, library, get_keyword_names)
        self._run_keyword = run_keyword
        self._supports_kwargs = self._get_kwargs_support(run_keyword)
        self._get_keyword_arguments = dynamic_method(library, "get_keyword_arguments")
        self._get_keyword_documentation = dynamic_method(
            library, "get_keyword_documentation"
        )
        self._get_keyword_tags = dynamic_method(library, "get_keyword_tags")
        self._get_keyword_types = dynamic_method(library, "get_keyword_types")
        self._get_library_information = dynamic_method(library, "get_library_information")

    def _get_kwargs_support(self, run_keyword):
        args = getfullargspec(run_keyword)[0]
        return len(args) > 3  # self, name, args, kwargs=None

    def run_keyword(self, name, args, kwargs=None):
        args = [name, args, kwargs] if kwargs else [name, args]
        return KeywordRunner(self._run_keyword).run_keyword(args)

    def get_keyword_arguments(self, name):
        if self._get_keyword_arguments:
            return self._get_keyword_arguments(name)
        if self._supports_kwargs:
            return ["*varargs", "**kwargs"]
        return ["*varargs"]

    def get_library_information(self):
        if self._get_library_information:
            return self._get_library_information()
        return {}
    
    def get_keyword_documentation(self, name):
        if self._get_keyword_documentation:
            return self._get_keyword_documentation(name)
        return ""

    def get_keyword_tags(self, name):
        if self._get_keyword_tags:
            return self._get_keyword_tags(name)
        return []

    def get_keyword_types(self, name):
        if self._get_keyword_types:
            return self._get_keyword_types(name)
        return None


class KeywordRunner(object):

    def __init__(self, keyword):
        self._keyword = keyword
        self._kw_name = self._keyword.__name__

    # def _add_to_queue_keyword(self, q: mp.Pipe, keyword, args, kwargs):
    #     r = keyword(*args, **kwargs)
    #     q.send(r)

    # def _cancel_run_keyword(self, p: mp.Process, force=False) -> bool:
    #     isalive_state = p.is_alive()

    #     if isalive_state:

    #         # Удаление всех дочерних процессов, чтобы не было зомби-процессов
    #         try:
    #             psu = psutil.Process(p.pid)
    #             if psu.is_running():
    #                 childs = psu.children()
    #                 if childs:
    #                     for child_pid in childs:
    #                         if child_pid.is_running():
    #                             child_pid.kill()
    #         except Exception as e:
    #             logging.info(e)

    #         # Полная остановка процесса (принудительная)
    #         try:
    #             if p.is_alive():
    #                 try:
    #                     p.join(3)
    #                     if not force:
    #                         p.terminate()
    #                     else:
    #                         p.kill()
    #                     p.close()
    #                 except Exception as e:
    #                     logging.info(e)
    #             else:
    #                 try:
    #                     p.close()
    #                 except Exception as e:
    #                     logging.info(e)

    #         except Exception as e:
    #             logging.info(e)
    #         if not force:
    #             raise RemoteError(
    #                 "The process executing the keyword exceeded the global timeout."
    #             )
    #     else:
    #         logging.info("The process does not need to be canceled")
    #     return True

    # def _run_keyword_as_process(self, args, kwargs):
    #     ctx = mp.get_context("spawn")
    #     parent_conn, child_conn = ctx.Pipe()
    #     p = ctx.Process(
    #         target=self._add_to_queue_keyword,
    #         args=(child_conn, self._keyword, args, kwargs),
    #         daemon=True,
    #     )

    #     p.start()
    #     p.join(GLOBAL_KEYWORDS_TIMEOUT)

    #     if not p.is_alive():
    #         try:
    #             return_value = parent_conn.recv()
    #             p.close()
    #         except Exception as e:
    #             logging.info(e)
    #     else:
    #         self._cancel_run_keyword(p)

    #     return return_value

    def run_keyword(self, args, kwargs=None):
        args = self._handle_binary(args)
        kwargs = self._handle_binary(kwargs or {})
        result = KeywordResult()
        with StandardStreamInterceptor() as interceptor:
            try:

                # TODO: При дисконекте отменять задачу?? sockerserver.sendall?? Обратный echo
                # TODO: Удаление всех задач открытых потоков?
                # Идея хорошая - но вызывает иногда неожиданные баги
                # if self._kw_name in [
                #     "run_and_return_rc_and_output",
                #     "run_and_return_rc",
                #     "run",
                # ]:
                # return_value = self._run_keyword_as_process(args, kwargs)
                #else:
                
                return_value = self._keyword(*args, **kwargs)

            except Exception:
                result.set_error(*sys.exc_info())
            else:
                try:
                    result.set_return(return_value)
                except Exception:
                    result.set_error(*sys.exc_info()[:2])
                else:
                    result.set_status("PASS")

        result.set_output(interceptor.output)
        return result.data

    def _handle_binary(self, arg):
        # No need to compare against other iterables or mappings because we
        # only get actual lists and dicts over XML-RPC. Binary cannot be
        # a dictionary key either.
        if isinstance(arg, list):
            return [self._handle_binary(item) for item in arg]
        if isinstance(arg, dict):
            return dict((key, self._handle_binary(arg[key])) for key in arg)
        if isinstance(arg, Binary):
            return arg.data
        return arg


class StandardStreamInterceptor(object):

    def __init__(self):
        self.output = ""
        self.origout = sys.stdout
        self.origerr = sys.stderr
        sys.stdout = StringIO()
        sys.stderr = StringIO()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        stdout = sys.stdout.getvalue()
        stderr = sys.stderr.getvalue()
        close = [sys.stdout, sys.stderr]
        sys.stdout = self.origout
        sys.stderr = self.origerr
        for stream in close:
            stream.close()
        if stdout and stderr:
            if not stderr.startswith(
                ("*TRACE*", "*DEBUG*", "*INFO*", "*HTML*", "*WARN*", "*ERROR*")
            ):
                stderr = "*INFO* %s" % stderr
            if not stdout.endswith("\n"):
                stdout += "\n"
        self.output = stdout + stderr


class KeywordResult(object):
    _generic_exceptions = (AssertionError, RuntimeError, Exception)

    def __init__(self):
        self.data = {"status": "FAIL"}

    def set_error(self, exc_type, exc_value, exc_tb=None):
        self.data["error"] = self._get_message(exc_type, exc_value)
        if exc_tb:
            self.data["traceback"] = self._get_traceback(exc_tb)
        continuable = self._get_error_attribute(exc_value, "CONTINUE")
        if continuable:
            self.data["continuable"] = continuable
        fatal = self._get_error_attribute(exc_value, "EXIT")
        if fatal:
            self.data["fatal"] = fatal

    def _get_message(self, exc_type, exc_value):
        name = exc_type.__name__
        message = self._get_message_from_exception(exc_value)
        if not message:
            return name
        if exc_type in self._generic_exceptions or getattr(
            exc_value, "ROBOT_SUPPRESS_NAME", False
        ):
            return message
        return "%s: %s" % (name, message)

    def _get_message_from_exception(self, value):
        # UnicodeError occurs if message contains non-ASCII bytes
        try:
            msg = unicode(value)
        except UnicodeError:
            msg = " ".join(self._str(a, handle_binary=False) for a in value.args)
        return self._handle_binary_result(msg)

    def _get_traceback(self, exc_tb):
        # Latest entry originates from this module so it can be removed
        entries = traceback.extract_tb(exc_tb)[1:]
        trace = "".join(traceback.format_list(entries))
        return "Traceback (most recent call last):\n" + trace

    def _get_error_attribute(self, exc_value, name):
        return bool(getattr(exc_value, "ROBOT_%s_ON_FAILURE" % name, False))

    def set_return(self, value):
        value = self._handle_return_value(value)
        if value != "":
            self.data["return"] = value

    def _handle_return_value(self, ret):
        if isinstance(ret, (str, unicode, bytes)):
            return self._handle_binary_result(ret)
        if isinstance(ret, (int, long, float)):
            return ret
        if isinstance(ret, Mapping):
            return dict(
                (self._str(key), self._handle_return_value(value))
                for key, value in ret.items()
            )
        try:
            return [self._handle_return_value(item) for item in ret]
        except TypeError:
            return self._str(ret)

    def _remove_ansi(self, str_with_ansi: str):

        # 7-bit C1 ANSI sequences
        ansi_escape = re.compile(
            r"""
                \x1B  # ESC
                (?:   # 7-bit C1 Fe (except CSI)
                    [@-Z\\-_]
                |     # or [ for CSI, followed by a control sequence
                    \[
                    [0-?]*  # Parameter bytes
                    [ -/]*  # Intermediate bytes
                    [@-~]   # Final byte
                )
            """,
            re.VERBOSE,
        )
        return ansi_escape.sub("", str_with_ansi)

    def _handle_binary_result(self, result):

        # for not use binary return
        if self._contains_binary(result) and isinstance(result, str):
            result = self._remove_ansi(result)

        if not self._contains_binary(result):
            return result

        if not isinstance(result, bytes):
            try:
                result = result.encode()
            except UnicodeError:
                try:
                    result = result.encode("ascii")
                except UnicodeError:
                    raise ValueError(
                        "Cannot represent %r as binary. Error decode" % result
                    )

        # With IronPython Binary cannot be sent if it contains "real" bytes.
        if sys.platform == "cli":
            result = str(result)
        return Binary(result)

    def _contains_binary(self, result):
        if PY3:
            return isinstance(result, bytes) or BINARY.search(result)
        return (
            isinstance(result, bytes)
            and NON_ASCII.search(result)
            or BINARY.search(result)
        )

    def _str(self, item, handle_binary=True):
        if item is None:
            return ""
        if not isinstance(item, (str, unicode, bytes)):
            item = unicode(item)
        if handle_binary:
            item = self._handle_binary_result(item)
        return item

    def set_status(self, status):
        self.data["status"] = status

    def set_output(self, output):
        if output:
            self.data["output"] = self._handle_binary_result(output)


def test_remote_server(uri, log=True):
    """Test is remote server running.

    :param uri:  Server address.
    :param log:  Log status message or not.
    :return      ``True`` if server is running, ``False`` otherwise.
    """
    logger = print if log else lambda message: None
    try:
        ServerProxy(uri).get_keyword_names()
    except Exception:
        logger("No remote server running at %s." % uri)
        return False
    logger("Remote server running at %s." % uri)
    return True


def stop_remote_server(uri, log=True):
    """Stop remote server unless server has disabled stopping.

    :param uri:  Server address.
    :param log:  Log status message or not.
    :return      ``True`` if server was stopped or it was not running in
                 the first place, ``False`` otherwise.
    """
    logger = print if log else lambda message: None
    if not test_remote_server(uri, log=False):
        logger("No remote server running at %s." % uri)
        return True
    logger("Stopping remote server at %s." % uri)
    if not ServerProxy(uri).stop_remote_server():
        logger("Stopping not allowed!")
        return False
    return True


if __name__ == "__main__":

    def parse_args(script, *args):
        actions = {"stop": stop_remote_server, "test": test_remote_server}
        if not (0 < len(args) < 3) or args[0] not in actions:
            sys.exit("Usage:  %s {test|stop} [uri]" % os.path.basename(script))
        uri = args[1] if len(args) == 2 else "http://127.0.0.1:8270"
        if "://" not in uri:
            uri = "http://" + uri
        return actions[args[0]], uri

    action, uri = parse_args(*sys.argv)
    success = action(uri)
    sys.exit(0 if success else 1)

