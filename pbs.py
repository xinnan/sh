#===============================================================================
# Copyright (C) 2011-2012 by Andrew Moffat
# 
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
# 
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
# 
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
#===============================================================================



import subprocess as subp
import sys
import traceback
import os
import re
from glob import glob
import shlex
from types import ModuleType
from functools import partial
import inspect


import pty
import termios
import signal
import atexit
import gc

from oproc import OProc


__version__ = "0.82"
__project_url__ = "https://github.com/amoffat/pbs"

IS_PY3 = sys.version_info[0] == 3
if IS_PY3: raw_input = input




class ErrorReturnCode(Exception):
    truncate_cap = 750

    def __init__(self, full_cmd, stdout, stderr):
        self.full_cmd = full_cmd
        self.stdout = stdout
        self.stderr = stderr


        if self.stdout is None: tstdout = "<redirected>"
        else:
            tstdout = self.stdout[:self.truncate_cap] 
            out_delta = len(self.stdout) - len(tstdout)
            if out_delta: 
                tstdout += "... (%d more, please see e.stdout)" % out_delta
            
        if self.stderr is None: tstderr = "<redirected>"
        else:
            tstderr = self.stderr[:self.truncate_cap]
            err_delta = len(self.stderr) - len(tstderr)
            if err_delta: 
                tstderr += "... (%d more, please see e.stderr)" % err_delta

        msg = "\n\n  RAN: %r\n\n  STDOUT:\n%s\n\n  STDERR:\n%s" %\
            (full_cmd, tstdout, tstderr)
        super(ErrorReturnCode, self).__init__(msg)

class CommandNotFound(Exception): pass

rc_exc_regex = re.compile("ErrorReturnCode_(\d+)")
rc_exc_cache = {}

def get_rc_exc(rc):
    rc = int(rc)
    try: return rc_exc_cache[rc]
    except KeyError: pass
    
    name = "ErrorReturnCode_%d" % rc
    exc = type(name, (ErrorReturnCode,), {})
    rc_exc_cache[rc] = exc
    return exc




def which(program):
    def is_exe(fpath):
        return os.path.exists(fpath) and os.access(fpath, os.X_OK)

    fpath, fname = os.path.split(program)
    if fpath:
        if is_exe(program): return program
    else:
        for path in os.environ["PATH"].split(os.pathsep):
            exe_file = os.path.join(path, program)
            if is_exe(exe_file):
                return exe_file

    return None

def resolve_program(program):
    path = which(program)
    if not path:
        # our actual command might have a dash in it, but we can't call
        # that from python (we have to use underscores), so we'll check
        # if a dash version of our underscore command exists and use that
        # if it does
        if "_" in program: path = which(program.replace("_", "-"))        
        if not path: return None
    return path







class RunningCommand(object):
    def __init__(self, cmd, call_args, stdin, stdout, stderr):
        
        self.call_args = call_args
        self.cmd = cmd
        self.process = None


        self.should_wait = True
        spawn_process = True


        # with contexts shouldn't run at all yet, they prepend
        # to every command in the context
        if call_args["with"]:
            spawn_process = False
            Command._prepend_stack.append(cmd)
            

        if callable(call_args["out"]) or callable(call_args["err"]):
            self.should_wait = False
            
        if call_args["piped"] or call_args["for"]:
            self.should_wait = False
            
        # we're running in the background, return self and let us lazily
        # evaluate
        if call_args["bg"]: self.should_wait = False

        
        if spawn_process:
            self.process = OProc(cmd, stdin, stdout, stderr,
                bufsize=call_args["bufsize"], wait=self.should_wait)
        

    def wait(self):
        exit_status = self.process.wait()

        if exit_status > 0: raise get_rc_exc(exit_status)(
                " ".join(self.cmd),
                self.process.stdout,
                self.process.stderr
            )        
        return self

    @property
    def stdout(self):
        self.wait()
        return self.process.stdout
    
    @property
    def stderr(self):
        self.wait()
        return self.process.stderr
    

    
    def __len__(self):
        return len(str(self))
    
    def __enter__(self):
        # we don't actually do anything here because anything that should
        # have been done would have been done in the Command.__call__ call.
        # essentially all that has to happen is the comand be pushed on
        # the prepend stack.
        pass
    
    def __iter__(self):
        return self
    
    def next(self):
        # we do this because if get blocks, we can't catch a KeyboardInterrupt
        # so the slight timeout allows for that.
        while True:
            try: chunk = self._pipe_queue.get(False, .001)
            except Empty: pass
            else:
                if chunk is None:
                    # we test to see if we're done before we stop iterating,
                    # and if we are don, wait for the other streams to finish
                    # writing their output to their buffers.
                    if self.done: self.wait()
                    raise StopIteration()
                return chunk
            
     # python 3
    __next__ = next

    def __exit__(self, typ, value, traceback):
        if self.call_args["with"] and Command._prepend_stack:
            Command._prepend_stack.pop()
   
    def __str__(self):
        if IS_PY3: return self.__unicode__()
        else: return unicode(self).encode("utf-8")
        
    def __unicode__(self):
        if self.process: 
            if self.stdout:
                if IS_PY3: return self.stdout
                return self.stdout.decode("utf-8") # byte string
            else: return ""

    def __eq__(self, other):
        return str(self) == str(other)

    def __contains__(self, item):
        return item in str(self)

    def __getattr__(self, p):
        # let these three attributes pass through to the Popen object
        if p in ("send_signal", "terminate", "kill"):
            if self.process: return getattr(self.process, p)
            else: raise AttributeError
        return getattr(str(self), p)
     
    def __repr__(self):
        return str(self)

    def __long__(self):
        return long(str(self).strip())

    def __float__(self):
        return float(str(self).strip())

    def __int__(self):
        return int(str(self).strip())






class BakedCommand(object):
    def __init__(self, cmd, attr):
        self._cmd = cmd
        self._attr = attr
        self._partial = partial(cmd, attr)
        
    def __call__(self, *args, **kwargs):
        return self._partial(*args, **kwargs)
        
    def __str__(self):
        if IS_PY3: return self.__unicode__()
        else: return unicode(self).encode("utf-8")

    def __repr__(self):
        return str(self)
        
    def __unicode__(self):
        return "%s %s" % (self._cmd, self._attr)



class Command(object):
    _prepend_stack = []
    
    _call_args = {
        "fg": False, # run command in foreground
        "bg": False, # run command in background
        "with": False, # prepend the command to every command after it
        "in": None,
        "out": None, # redirect STDOUT
        "err": None, # redirect STDERR
        "err_to_out": None, # redirect STDERR to STDOUT
        "bufsize": 1,
        
        # comment
        "piped": None,
        "for": None,
    }
    
    # these are arguments that cannot be called together, because they wouldn't
    # make anys ense
    _incompatible_call_args = (
        ("fg", "bg", "Command can't be run in the foreground and background"),
        ("err", "err_to_out", "Stderr is already being redirected"),
        ("piped", "for", "You cannot iterate when this command is being piped"),
    )

    @classmethod
    def _create(cls, program):
        path = resolve_program(program)
        if not path: raise CommandNotFound(program)
        return cls(path)
    
    def __init__(self, path):            
        self._path = path
        self._partial = False
        self._partial_baked_args = []
        self._partial_call_args = {}
        
    def __getattribute__(self, name):
        # convenience
        getattr = partial(object.__getattribute__, self)
        
        # the logic here is, if an attribute starts with an
        # underscore, always try to find it, because it's very unlikely
        # that a first command will start with an underscore, example:
        # "git _command" will probably never exist.

        # after that, we check to see if the attribute actually exists
        # on the Command object, but only return that if we're not
        # a baked object.
        if name.startswith("_"): return getattr(name)
        try: attr = getattr(name)
        except AttributeError: return BakedCommand(self, name)

        if self._partial: return BakedCommand(self, name)
        return attr

    
    @staticmethod
    def _extract_call_args(kwargs, to_override={}):
        kwargs = kwargs.copy()
        call_args = {}
        for parg, default in Command._call_args.items():
            key = "_" + parg
            
            if key in kwargs:
                call_args[parg] = kwargs[key] 
                del kwargs[key]
            elif parg in to_override:
                call_args[parg] = to_override[parg]
        
        # test for incompatible call args
        s1 = set(call_args.keys())
        for args in Command._incompatible_call_args:
            args = list(args)
            error = args.pop()

            if s1.issuperset(args):
                raise TypeError("Invalid special arguments %r: %s" % (args, error))
            
        return call_args, kwargs


    def _format_arg(self, arg):
        if IS_PY3: arg = str(arg)
        else: arg = unicode(arg).encode("utf8")
        arg = '"%s"' % arg

        return arg

    def _compile_args(self, args, kwargs):
        processed_args = []
                
        # aggregate positional args
        for arg in args:
            if isinstance(arg, (list, tuple)):
                for sub_arg in arg: processed_args.append(self._format_arg(sub_arg))
            else: processed_args.append(self._format_arg(arg))

        # aggregate the keyword arguments
        for k,v in kwargs.items():
            # we're passing a short arg as a kwarg, example:
            # cut(d="\t")
            if len(k) == 1:
                if v is True: arg = "-"+k
                else: arg = "-%s %s" % (k, self._format_arg(v))

            # we're doing a long arg
            else:
                k = k.replace("_", "-")

                if v is True: arg = "--"+k
                else: arg = "--%s=%s" % (k, self._format_arg(v))
            processed_args.append(arg)

        try: processed_args = shlex.split(" ".join(processed_args))
        except ValueError, e:
            if str(e) == "No closing quotation":
                exc_msg = """No closing quotation.  If you're trying to escape \
double quotes, please note that you need to escape the escape:

    # incorrect
    print pbs.echo('test print double quote: \"')
    print pbs.echo('test print double quote: \\"')
    print pbs.echo("test print double quote: \\"")
    print pbs.echo("test print double quote: \\\\"")

    # correct
    print pbs.echo('test print double quote: \\\\"')
    print pbs.echo("test print double quote: \\\\\\"")
"""
                raise ValueError(exc_msg)
        return processed_args
 
    
    def bake(self, *args, **kwargs):
        fn = Command(self._path)
        fn._partial = True

        fn._partial_call_args, kwargs = self._extract_call_args(kwargs)
        processed_args = self._compile_args(args, kwargs)
        fn._partial_baked_args = processed_args
        return fn
       
    def __str__(self):
        if IS_PY3: return self.__unicode__()
        else: return unicode(self).encode("utf-8")
        
    def __eq__(self, other):
        try: return str(self) == str(other)
        except: return False

    def __repr__(self):
        return str(self)
        
    def __unicode__(self):
        baked_args = " ".join(self._partial_baked_args)
        if baked_args: baked_args = " " + baked_args
        return self._path + baked_args

    def __enter__(self):
        Command._prepend_stack.append([self._path])

    def __exit__(self, typ, value, traceback):
        Command._prepend_stack.pop()
            
    
    def __call__(self, *args, **kwargs):
        kwargs = kwargs.copy()
        args = list(args)

        cmd = []

        # aggregate any with contexts
        for prepend in self._prepend_stack: cmd.extend(prepend)

        cmd.append(self._path)
        

        # here we extract the special kwargs and override any
        # special kwargs from the possibly baked command
        tmp_call_args, kwargs = self._extract_call_args(kwargs, self._partial_call_args)
        call_args = Command._call_args.copy()
        call_args.update(tmp_call_args)

        
        # check if we're piping via composition
        stdin = None
        if args:
            first_arg = args.pop(0)
            if isinstance(first_arg, RunningCommand):
                # it makes sense that if the input pipe of a command is running
                # in the background, then this command should run in the
                # background as well
                if first_arg.call_args["bg"]: call_args["bg"] = True
                stdin = first_arg.process._pipe_queue
                
            else: args.insert(0, first_arg)
            
        processed_args = self._compile_args(args, kwargs)

        # makes sure our arguments are broken up correctly
        split_args = self._partial_baked_args + processed_args

        # we used to glob, but now we don't.  the reason being, escaping globs
        # doesn't work.  also, adding a _noglob attribute doesn't allow the
        # flexibility to glob some args and not others.  so we have to leave
        # the globbing up to the user entirely
        #=======================================================================
        # # now glob-expand each arg and compose the final list
        # final_args = []
        # for arg in split_args:
        #    expanded = glob(arg)
        #    if expanded: final_args.extend(expanded)
        #    else: final_args.append(arg)
        #=======================================================================
        final_args = split_args

        cmd.extend(final_args)



        # stdout redirection
        stdout = call_args["out"]
        if stdout and not callable(stdout) and not hasattr(out, "write"):
            stdout = file(str(stdout), "w")
        

        # stderr redirection
        stderr = call_args["err"]
        if stderr and not callable(stderr) and not hasattr(err, "write"):
            stderr = file(str(err), "w")
            
        #if call_args["err_to_out"]: stderr = subp.STDOUT

        return RunningCommand(cmd, call_args, stdin, stdout, stderr)







# this class is used directly when we do a "from pbs import *".  it allows
# lookups to names that aren't found in the global scope to be searched
# for as a program.  for example, if "ls" isn't found in the program's
# scope, we consider it a system program and try to find it.
class Environment(dict):
    def __init__(self, *args, **kwargs):
        dict.__init__(self, *args, **kwargs)
        
        self["Command"] = Command
        self["CommandNotFound"] = CommandNotFound
        self["ErrorReturnCode"] = ErrorReturnCode
        self["ARGV"] = sys.argv[1:]
        for i, arg in enumerate(sys.argv):
            self["ARG%d" % i] = arg
        
        # this needs to be last
        self["env"] = os.environ
        
    def __setitem__(self, k, v):
        # are we altering an environment variable?
        if "env" in self and k in self["env"]: self["env"][k] = v
        # no?  just setting a regular name
        else: dict.__setitem__(self, k, v)
        
    def __missing__(self, k):
        # the only way we'd get to here is if we've tried to
        # import * from a repl.  so, raise an exception, since
        # that's really the only sensible thing to do
        if k == "__all__":
            raise ImportError("Cannot import * from pbs. \
Please import pbs or import programs individually.")

        # if we end with "_" just go ahead and skip searching
        # our namespace for python stuff.  this was mainly for the
        # command "id", which is a popular program for finding
        # if a user exists, but also a python function for getting
        # the address of an object.  so can call the python
        # version by "id" and the program version with "id_"
        if not k.endswith("_"):
            # check if we're naming a dynamically generated ReturnCode exception
            try: return rc_exc_cache[k]
            except KeyError:
                m = rc_exc_regex.match(k)
                if m: return get_rc_exc(int(m.group(1)))
                
            # are we naming a commandline argument?
            if k.startswith("ARG"):
                return None
                
            # is it a builtin?
            try: return getattr(self["__builtins__"], k)
            except AttributeError: pass
        elif not k.startswith("_"): k = k.rstrip("_")
        
        # how about an environment variable?
        try: return os.environ[k]
        except KeyError: pass
        
        # is it a custom builtin?
        builtin = getattr(self, "b_"+k, None)
        if builtin: return builtin
        
        # it must be a command then
        return Command._create(k)
    
    def b_cd(self, path):
        os.chdir(path)
        
    def b_which(self, program):
        return which(program)





def run_repl(env):
    banner = "\n>> PBS v{version}\n>> https://github.com/amoffat/pbs\n"
    
    print(banner.format(version=__version__))
    while True:
        try: line = raw_input("pbs> ")
        except (ValueError, EOFError): break
            
        try: exec(compile(line, "<dummy>", "single"), env, env)
        except SystemExit: break
        except: print(traceback.format_exc())

    # cleans up our last line
    print("")




# this is a thin wrapper around THIS module (we patch sys.modules[__name__]).
# this is in the case that the user does a "from pbs import whatever"
# in other words, they only want to import certain programs, not the whole
# system PATH worth of commands.  in this case, we just proxy the
# import lookup to our Environment class
class SelfWrapper(ModuleType):
    def __init__(self, self_module):
        # this is super ugly to have to copy attributes like this,
        # but it seems to be the only way to make reload() behave
        # nicely.  if i make these attributes dynamic lookups in
        # __getattr__, reload sometimes chokes in weird ways...
        for attr in ["__builtins__", "__doc__", "__name__", "__package__"]:
            setattr(self, attr, getattr(self_module, attr))

        self.self_module = self_module
        self.env = Environment(globals())
    
    def __getattr__(self, name):
        return self.env[name]





# we're being run as a stand-alone script, fire up a REPL
if __name__ == "__main__":
    globs = globals()
    f_globals = {}
    for k in ["__builtins__", "__doc__", "__name__", "__package__"]:
        f_globals[k] = globs[k]
    env = Environment(f_globals)
    run_repl(env)
    
# we're being imported from somewhere
else:
    self = sys.modules[__name__]
    sys.modules[__name__] = SelfWrapper(self)
