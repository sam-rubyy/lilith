"""A small interpreted Python subset for pure JSON tools. Never calls eval/exec.

Generated code has no Python objects, imports, attributes, filesystem, network,
processes, reflection, or ambient builtins. Every AST operation is metered.
"""
import ast
import json
import math
import operator
import time


class SandboxError(ValueError):
    pass


CONTRACT = """Define exactly one function: def run(data): returning JSON-compatible data.
No imports, attributes/method calls, classes, decorators, globals, recursion, lambdas,
exceptions, while loops, comprehensions, or file/network/process access.
Supported statements: local assignments (names only), for NAME in LIST, if/else,
return, break, continue, and expression statements. Supported expressions: literals,
list/dict/tuple, indexing and slicing, + - * / // %, comparisons, and/or/not,
conditional expressions, and calls to these named helpers:
len, str, int, float, bool, abs, round, min, max, sum, sorted, range, enumerate,
get(object,key,default=None), keys(object), values(object), items(object),
lower(text), upper(text), strip(text), split(text,separator), join(separator,items),
replace(text,old,new), json_loads(text), json_dumps(data), fail(message).
Build lists with result = result + [item]. Input/output JSON <= 64 KiB, collections
<= 1000 items, nesting <= 20, integers <= 256 bits, 20000 operations, 2 seconds.
Use fail('message') to reject invalid values. Inputs/outputs use simple JSON Schema.
The runtime validates inputs against the input schema BEFORE run(data). Do not
repeat type checking in code: isinstance, type, try/except, and raise are unavailable.
Example: def run(data): return {'total': data['value'] * 2}
"""

BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
       ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod}
CMP = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt, ast.LtE: operator.le,
       ast.Gt: operator.gt, ast.GtE: operator.ge, ast.In: lambda a, b: a in b, ast.NotIn: lambda a, b: a not in b,
       ast.Is: operator.is_, ast.IsNot: operator.is_not}
HELPERS = {"len", "str", "int", "float", "bool", "abs", "round", "min", "max", "sum", "sorted", "range", "enumerate",
           "get", "keys", "values", "items", "lower", "upper", "strip", "split", "join", "replace", "json_loads", "json_dumps", "fail"}
HELPERS.add("shell")

SHELL_CONTRACT = """Owner-granted extension: shell(command) runs a command string through the
system shell with the owner's full filesystem, process, and network permissions.
There is no command allowlist or workspace confinement. The starting directory is
the workspace. Return value: {'returncode': integer, 'output': combined stdout/stderr}.
Use get(result, 'returncode') and get(result, 'output'); attributes remain unavailable.
Use shell for host tasks, including installed programs or Python through python -c.
Never claim a command succeeded without checking returncode. Jobs remain cancellable;
each command has a 120-second timeout and bounded captured output.
For tests, provide shell_calls: [{"command": "exact expected command", "result":
{"returncode": 0, "output": "fake output"}}]. These are ordered mocks, not commands
to execute. Include a nonzero returncode test. Only final execution uses a real shell.
Do not interpolate untrusted strings into commands without correct shell quoting.
"""
NODES = {ast.Module, ast.FunctionDef, ast.arguments, ast.arg, ast.Assign, ast.For, ast.If, ast.Return,
         ast.Break, ast.Continue, ast.Expr, ast.Name, ast.Load, ast.Store, ast.Constant, ast.List,
         ast.Tuple, ast.Dict, ast.Subscript, ast.Slice, ast.BinOp, ast.UnaryOp, ast.UAdd, ast.USub,
         ast.Not, ast.Compare, ast.BoolOp, ast.And, ast.Or, ast.IfExp, ast.Call, *BIN, *CMP}


def bounded(value):
    count = 0

    def visit(v, depth):
        nonlocal count
        count += 1
        if count > 6000 or depth > 20:
            raise SandboxError("JSON structure budget exceeded")
        if type(v) in {list, tuple, dict}:
            if len(v) > 1000:
                raise SandboxError("Collection budget exceeded")
            if isinstance(v, dict):
                if any(type(k) is not str for k in v):
                    raise SandboxError("JSON object keys must be strings")
                for key, item in v.items():
                    visit(key, depth + 1)
                    visit(item, depth + 1)
            else:
                for item in v:
                    visit(item, depth + 1)
        elif type(v) is str:
            if len(v) > 65536:
                raise SandboxError("String budget exceeded")
        elif type(v) is int:
            if v.bit_length() > 256:
                raise SandboxError("Integer budget exceeded")
        elif type(v) is float:
            if not math.isfinite(v):
                raise SandboxError("Non-finite numbers are not allowed")
        elif v is not None and type(v) is not bool:
            raise SandboxError("Only JSON-compatible values are allowed")

    visit(value, 0)
    if len(json.dumps(value, allow_nan=False).encode()) > 65536:
        raise SandboxError("JSON byte budget exceeded")
    return value


def validate_source(source):
    if not isinstance(source, str) or len(source.encode()) > 32768:
        raise SandboxError("Source budget exceeded")
    try:
        tree = ast.parse(source)
    except (SyntaxError, RecursionError, ValueError) as error:
        raise SandboxError(f"Invalid source: {error}") from error
    if len(tree.body) != 1 or type(tree.body[0]) is not ast.FunctionDef:
        raise SandboxError("Define exactly one run(data) function")
    function = tree.body[0]
    args = function.args
    if (function.name != "run" or function.decorator_list or function.returns or function.type_params
            or len(args.args) != 1 or args.args[0].arg != "data" or args.args[0].annotation
            or args.posonlyargs or args.kwonlyargs or args.vararg or args.kwarg or args.defaults or args.kw_defaults):
        raise SandboxError("Entry point must be undecorated run(data), without annotations/defaults")
    nodes = list(ast.walk(tree))
    if len(nodes) > 1500:
        raise SandboxError("AST budget exceeded")
    local_names = {"data"} | {n.id for n in nodes if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
    if local_names & HELPERS:
        raise SandboxError("Local names cannot shadow sandbox helpers")
    for node in nodes:
        if type(node) not in NODES:
            raise SandboxError(f"Unsupported syntax: {type(node).__name__}")
        if isinstance(node, ast.FunctionDef) and node is not function:
            raise SandboxError("Nested functions are forbidden")
        if isinstance(node, ast.Name) and (node.id.startswith("_") or node.id == "run"):
            raise SandboxError("Private names/recursion are forbidden")
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id not in local_names | HELPERS:
            raise SandboxError(f"Unknown name: {node.id}")
        if isinstance(node, ast.Call) and (type(node.func) is not ast.Name or node.func.id not in HELPERS or node.keywords):
            raise SandboxError("Only named sandbox helpers with positional arguments are callable")
        if isinstance(node, ast.Assign) and (len(node.targets) != 1 or type(node.targets[0]) is not ast.Name):
            raise SandboxError("Assign to one local name only")
        if isinstance(node, ast.For) and type(node.target) is not ast.Name:
            raise SandboxError("Loop target must be one local name")
        if isinstance(node, ast.Dict) and any(k is None for k in node.keys):
            raise SandboxError("Dictionary unpacking is forbidden")
    return tree


def formatted_source(source):
    return ast.unparse(validate_source(source)) + "\n"


class Returned(Exception):
    def __init__(self, value):
        self.value = value


class LoopBreak(Exception):
    pass


class LoopContinue(Exception):
    pass


class Sandbox:
    def __init__(self, *, operations=20000, seconds=2, shell=None):
        self.limit = min(20000, operations)
        self.seconds = min(2, seconds)
        self.shell = shell

    def tick(self):
        self.remaining -= 1
        if self.remaining < 0 or time.monotonic() > self.deadline:
            raise SandboxError("Sandbox execution budget exceeded")

    def run(self, source, data):
        bounded(data)
        tree = validate_source(source)
        self.remaining = self.limit
        self.deadline = time.monotonic() + self.seconds
        self.env = {"data": json.loads(json.dumps(data))}
        try:
            self.block(tree.body[0].body)
        except Returned as returned:
            return json.loads(json.dumps(bounded(returned.value)))
        except SandboxError:
            raise
        except (ValueError, TypeError, KeyError, IndexError, ZeroDivisionError, OverflowError, LoopBreak, LoopContinue) as error:
            raise SandboxError(f"Tool rejected input: {type(error).__name__}: {error}") from error
        raise SandboxError("Tool did not return a result")

    def block(self, statements):
        for node in statements:
            self.tick()
            if isinstance(node, ast.Return):
                raise Returned(self.expr(node.value) if node.value else None)
            if isinstance(node, ast.Assign):
                self.env[node.targets[0].id] = self.expr(node.value)
                bounded(self.env)
            elif isinstance(node, ast.If):
                self.block(node.body if self.expr(node.test) else node.orelse)
            elif isinstance(node, ast.For):
                iterable = self.expr(node.iter)
                if type(iterable) not in {list, tuple, dict, str} or len(iterable) > 1000:
                    raise SandboxError("Invalid bounded loop iterable")
                broken = False
                for item in iterable:
                    self.tick()
                    self.env[node.target.id] = item
                    try:
                        self.block(node.body)
                    except LoopContinue:
                        continue
                    except LoopBreak:
                        broken = True
                        break
                if not broken:
                    self.block(node.orelse)
            elif isinstance(node, ast.Break):
                raise LoopBreak()
            elif isinstance(node, ast.Continue):
                raise LoopContinue()
            elif isinstance(node, ast.Expr):
                self.expr(node.value)

    def expr(self, node):
        self.tick()
        if isinstance(node, ast.Constant):
            value = node.value
        elif isinstance(node, ast.Name):
            value = self.env[node.id]
        elif isinstance(node, (ast.List, ast.Tuple)):
            value = [self.expr(n) for n in node.elts]
        elif isinstance(node, ast.Dict):
            value = {self.expr(k): self.expr(v) for k, v in zip(node.keys, node.values)}
        elif isinstance(node, ast.Subscript):
            obj = self.expr(node.value)
            if isinstance(node.slice, ast.Slice):
                sl = node.slice
                index = slice(*(self.expr(n) if n else None for n in (sl.lower, sl.upper, sl.step)))
            else:
                index = self.expr(node.slice)
            value = obj[index]
        elif isinstance(node, ast.BinOp):
            left, right = self.expr(node.left), self.expr(node.right)
            if isinstance(node.op, ast.Mult):
                sequence, factor = (left, right) if type(left) in {str, list} else (right, left)
                if type(sequence) in {str, list} and type(factor) is int and len(sequence) * max(0, factor) > 1000:
                    raise SandboxError("Sequence multiplication budget exceeded")
            if isinstance(node.op, ast.Mod) and type(left) is str:
                raise SandboxError("String formatting is not supported")
            value = BIN[type(node.op)](left, right)
        elif isinstance(node, ast.UnaryOp):
            item = self.expr(node.operand)
            value = not item if isinstance(node.op, ast.Not) else -item if isinstance(node.op, ast.USub) else +item
        elif isinstance(node, ast.BoolOp):
            value = self.expr(node.values[0])
            for n in node.values[1:]:
                if (isinstance(node.op, ast.And) and not value) or (isinstance(node.op, ast.Or) and value):
                    break
                value = self.expr(n)
        elif isinstance(node, ast.Compare):
            left = self.expr(node.left)
            value = True
            for op, other in zip(node.ops, node.comparators):
                right = self.expr(other)
                if not CMP[type(op)](left, right):
                    value = False
                    break
                left = right
        elif isinstance(node, ast.IfExp):
            value = self.expr(node.body if self.expr(node.test) else node.orelse)
        elif isinstance(node, ast.Call):
            value = self.call(node.func.id, [self.expr(a) for a in node.args])
        else:
            raise SandboxError(f"Unsupported expression: {type(node).__name__}")
        return bounded(value)

    def call(self, name, args):
        if name == "shell":
            if self.shell is None:
                raise SandboxError("This tool has no shell grant")
            if len(args) != 1 or not isinstance(args[0], str):
                raise SandboxError("shell expects one command string")
            started = time.monotonic()
            try:
                return self.shell(args[0])
            finally:
                # The broker and supervisor bound host work separately from AST CPU time.
                self.deadline += time.monotonic() - started
        safe = {"len": len, "str": str, "int": int, "float": float, "bool": bool, "abs": abs,
                "round": round, "min": min, "max": max, "sum": sum, "sorted": sorted}
        if name in safe:
            return safe[name](*args)
        if name == "range":
            result = range(*args)
            if len(result) > 1000:
                raise SandboxError("Range budget exceeded")
            return list(result)
        if name == "enumerate":
            return [list(pair) for pair in enumerate(*args)]
        if name == "get":
            obj, key, *default = args
            if type(obj) is not dict or len(default) > 1:
                raise SandboxError("get expects an object")
            return obj.get(key, default[0] if default else None)
        if name in {"keys", "values", "items"}:
            obj, = args
            if type(obj) is not dict:
                raise SandboxError("Object helper expects an object")
            return list(obj.keys()) if name == "keys" else list(obj.values()) if name == "values" else [list(p) for p in obj.items()]
        if name in {"lower", "upper", "strip", "split", "replace", "join"}:
            text, *rest = args
            if type(text) is not str:
                raise SandboxError("Text helper expects a string")
            if name == "replace":
                if len(rest) != 2 or not all(type(x) is str for x in rest):
                    raise SandboxError("replace expects three strings")
                occurrences = text.count(rest[0])
                if len(text) + occurrences * max(0, len(rest[1]) - len(rest[0])) > 65536:
                    raise SandboxError("Replacement budget exceeded")
            if name == "join":
                if len(rest) != 1 or type(rest[0]) is not list or any(type(x) is not str for x in rest[0]):
                    raise SandboxError("join expects separator and a list of strings")
                if sum(map(len, rest[0])) + len(text) * len(rest[0]) > 65536:
                    raise SandboxError("Join budget exceeded")
            return getattr(text, name)(*rest)
        if name == "json_loads":
            return json.loads(*args)
        if name == "json_dumps":
            return json.dumps(*args, allow_nan=False)
        if name == "fail":
            raise SandboxError(str(args[0])[:500] if args else "Input rejected")
        raise SandboxError("Unknown helper")
