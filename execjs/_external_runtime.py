from subprocess import Popen, PIPE
import io
import json
import os
import os.path
import platform
import re
import stat
import sys
import tempfile
import six
if platform.system() == 'Windows':
    import wexpect as pyexpect
else: 
    import pexpect as pyexpect
import execjs._json2 as _json2
import execjs._runner_sources as _runner_sources

from execjs._exceptions import (
    ProcessExitedWithNonZeroStatus,
    ProgramError
)

from execjs._abstract_runtime import AbstractRuntime
from execjs._abstract_runtime_context import AbstractRuntimeContext
from execjs._misc import encode_unicode_codepoints


class ExternalRuntime(AbstractRuntime):
    '''Runtime to execute codes with external command.'''
    def __init__(self, name, command, runner_source, encoding='utf8', tempfile=False, prompt='> '):
        self._name = name
        if isinstance(command, str):
            command = [command]
        self._command = command
        self._runner_source = runner_source
        self._encoding = encoding
        self._tempfile = tempfile
        self._session = None
        self._prompt = prompt

        self._available = self._binary() is not None

    def __str__(self):
        return "{class_name}({runtime_name})".format(
            class_name=type(self).__name__,
            runtime_name=self._name,
        )

    @property
    def name(self):
        return self._name

    def is_available(self):
        return self._available

    def session(self):
        cmd = self._binary()
        self._session = pyexpect.spawn(cmd)
        self._session.expect(self._prompt)
        self._session.delaybeforesend = None
        return self._session

    def _compile(self, source, cwd=None):
        return self.Context(self, source, cwd=cwd, tempfile=self._tempfile, session=self._session)

    def _binary(self):
        if not hasattr(self, "_binary_cache"):
            self._binary_cache = _which(self._command)
        return self._binary_cache

    class Context(AbstractRuntimeContext):
        # protected

        def __init__(self, runtime, source='', cwd=None, tempfile=False, session=False):
            self._runtime = runtime
            self._source = source
            self._cwd = cwd
            self._tempfile = tempfile
            self._session = session

        def is_available(self):
            return self._runtime.is_available()

        def _eval(self, source):
            if not source.strip():
                data = "''"
            else:
                data = "'('+" + json.dumps(source, ensure_ascii=True) + "+')'"

            code = 'return eval({data})'.format(data=data)
            return self.exec_(code)

        def _exec_(self, source):
            if self._source:
                source = self._source + '\n' + source

            if self._tempfile:
                output = self._exec_with_tempfile(source)
            elif self._session:
                output = self._exec_with_session(source)
            else:
                output = self._exec_with_pipe(source)
            return self._extract_result(output)

        def _call(self, identifier, *args):
            args = json.dumps(args)
            return self._eval("{identifier}.apply(this, {args})".format(identifier=identifier, args=args))

        def _exec_with_session(self, source):
            input = self._compile(source)
            end_str = '//!!@@##'
            self._session.sendline(input + '\n' + end_str)
            self._session.expect(end_str)
            stdoutdata = self._session.before if platform.system() == 'Windows' else self._session.before.decode()
            return stdoutdata

        def _exec_with_pipe(self, source):
            cmd = self._runtime._binary()

            p = None
            try:
                # print(cmd)
                p = Popen(cmd, stdin=PIPE, stdout=PIPE, stderr=PIPE, cwd=self._cwd, universal_newlines=True)
                input = self._compile(source)
                if six.PY2:
                    input = input.encode(sys.getfilesystemencoding())
                # print(input)
                input = ''.join(input.splitlines()) if 'deno' in cmd[0] else input
                stdoutdata, stderrdata = p.communicate(input=input)
                ret = p.wait()
                # print('stdout:', stdoutdata)
            finally:
                del p

            self._fail_on_non_zero_status(ret, stdoutdata, stderrdata)
            return stdoutdata

        def _exec_with_tempfile(self, source):
            (fd, filename) = tempfile.mkstemp(prefix='execjs', suffix='.js')
            os.close(fd)
            try:
                with io.open(filename, "w+", encoding=self._runtime._encoding) as fp:
                    fp.write(self._compile(source))
                cmd = self._runtime._binary() + [filename]

                p = None
                try:
                    p = Popen(cmd, stdout=PIPE, stderr=PIPE, cwd=self._cwd, universal_newlines=True)
                    stdoutdata, stderrdata = p.communicate()
                    ret = p.wait()
                finally:
                    del p

                self._fail_on_non_zero_status(ret, stdoutdata, stderrdata)
                return stdoutdata
            finally:
                os.remove(filename)

        def _fail_on_non_zero_status(self, status, stdoutdata, stderrdata):
            if status != 0:
                raise ProcessExitedWithNonZeroStatus(status=status, stdout=stdoutdata, stderr=stderrdata)

        def _compile(self, source):
            runner_source = self._runtime._runner_source

            replacements = {
                '#{source}': lambda: source,
                '#{encoded_source}': lambda: json.dumps(
                    "(function(){ " +
                    encode_unicode_codepoints(source) +
                    " })()"
                ),
                '#{json2_source}': _json2._json2_source,
            }

            pattern = "|".join(re.escape(k) for k in replacements)

            runner_source = re.sub(pattern, lambda m: replacements[m.group(0)](), runner_source)

            return runner_source

        def _extract_result(self, output):
            output = output.replace("\r\n", "\n").replace("\r", "\n")
            # print('outout:', output)
            output_last_line = [line for line in output.splitlines() if ']' in line][-1]
            # print(repr(output_last_line))
            ret = json.loads(output_last_line)
            if len(ret) == 1:
                ret = [ret[0], None]
            status, value = ret

            if status == "ok":
                return value
            else:
                raise ProgramError(value)


def _is_windows():
    """protected"""
    return platform.system() == 'Windows'


def _decode_if_not_text(s):
    """protected"""
    if isinstance(s, six.text_type):
        return s
    return s.decode(sys.getfilesystemencoding())


def _find_executable(prog, pathext=("",)):
    """protected"""
    pathlist = _decode_if_not_text(os.environ.get('PATH', '')).split(os.pathsep)

    for dir in pathlist:
        for ext in pathext:
            filename = os.path.join(dir, prog + ext)
            try:
                st = os.stat(filename)
            except os.error:
                continue
            if stat.S_ISREG(st.st_mode) and (stat.S_IMODE(st.st_mode) & 0o111):
                return filename
    return None


def _which(command):
    """protected"""
    if isinstance(command, str):
        command = [command]
    command = list(command)
    name = command[0]
    args = command[1:]

    if _is_windows():
        pathext = _decode_if_not_text(os.environ.get("PATHEXT", ""))
        path = _find_executable(name, pathext.split(os.pathsep))
    else:
        path = _find_executable(name)

    if not path:
        return None
    return [path] + args


def node():
    r = node_node()
    if r.is_available():
        return r
    return node_nodejs()


def node_node():
    return ExternalRuntime(
        name="Node.js (V8)",
        command=['node'],
        encoding='UTF-8',
        runner_source=_runner_sources.Node,
        prompt='> '
    )


def node_nodejs():
    return ExternalRuntime(
        name="Node.js (V8)",
        command=['nodejs'],
        encoding='UTF-8',
        runner_source=_runner_sources.Node,
        prompt='> '
    )


def deno():
    return ExternalRuntime(
        name="Deno",
        command=['deno'],
        encoding='UTF-8',
        runner_source=_runner_sources.Deno,
        prompt='> '
    )


def jsc():
    return ExternalRuntime(
        name="JavaScriptCore",
        command=["/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Resources/jsc"],
        runner_source=_runner_sources.JavaScriptCore,
        tempfile=True
    )


def spidermonkey():
    return ExternalRuntime(
        name="SpiderMonkey",
        command=["js"],
        runner_source=_runner_sources.SpiderMonkey,
        tempfile=True
    )


def jscript():
    return ExternalRuntime(
        name="JScript",
        command=["cscript", "//E:jscript", "//Nologo"],
        encoding="ascii",
        runner_source=_runner_sources.JScript,
        tempfile=True
    )


def phantomjs():
    return ExternalRuntime(
        name="PhantomJS",
        command=["phantomjs"],
        runner_source=_runner_sources.PhantomJS,
        tempfile=True
    )


def slimerjs():
    return ExternalRuntime(
        name="SlimerJS",
        command=["slimerjs"],
        runner_source=_runner_sources.SlimerJS,
        tempfile=True
    )


def nashorn():
    return ExternalRuntime(
        name="Nashorn",
        command=["jjs"],
        runner_source=_runner_sources.Nashorn,
        tempfile=True
    )


def llrt():
    return ExternalRuntime(
        name="Llrt",
        command=['llrt'],
        encoding='UTF-8',
        runner_source=_runner_sources.Llrt,
        tempfile=True
    )