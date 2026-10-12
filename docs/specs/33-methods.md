# rw methods on records

## Context

Records (spec 20) have fields but no behavior, so code that belongs to a
type is written as free functions taking it as the first argument. Issue
#10 asks for methods; Python-flavored code expects them:

```rw
class Counter:
    count: int

    def inc(self, by: int) -> int:
        self.count += by
        return self.count

    def reset(self) -> void:
        self.count = 0

def main() -> int:
    c = Counter(0)
    c.inc(2)
    c.inc(3)
    print(c.count)          # 5
    return 0
```

## Goals

- `def name(self, params) -> R:` inside a `class` body, after or between
  fields; `self` is written without a type and has the class's type
- Calls `obj.m(args)` on any expression of the class type (a local, a field
  chain, an element, a call result), including classes imported from
  another module
- Methods may call other methods (`self.other()`), read and write fields
  through `self`, and return any type

## Non-Goals

- Constructors with logic (`__init__`), static / class methods, properties
- Inheritance and overriding (#104 covers polymorphism)
- A method as a value (`f = c.inc`) or `spawn obj.m()`
- Overloading

## Design

The parser lowers each method to an ordinary top-level function named
`Class.m` with an explicit first parameter `self: Class`. `.` cannot occur
in an rw identifier, so these names never collide with user functions, and
the function symbol is `rw_user_[module.]Class.m`. Declaration, body
checking, desugaring, and code generation then need nothing new.

Sema records each class's methods (`ClassType.methods: name -> FuncKey`)
and resolves calls:

| Call shape | Resolution |
|---|---|
| `x.m(args)` with `x` a local (parsed as a qualified call) | method `m` of x's class, else a function-valued field `m` (spec 21) |
| `e.m(args)` for any other `e` (parsed as a call of `e.m`) | method `m` of e's class, else a function-valued field |

A method call checks `args` against the method's parameters after `self`
and is emitted as a direct call with the receiver as the first argument. A
class may not have a field and a method with the same name.

Layers touched: parser / sema / irgen (3).
