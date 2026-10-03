; Namespaces are not captured: block and file-scoped forms would otherwise
; give the same type different qualified names, and Go/TS drop the package too.

; `record struct` is listed before the plain record pattern: both match the
; same node at equal rank, and the first one recorded wins.
(record_declaration "struct" name: (identifier) @name) @def.struct
(record_declaration name: (identifier) @name) @def.class
(class_declaration name: (identifier) @name) @def.class
(struct_declaration name: (identifier) @name) @def.struct
(interface_declaration name: (identifier) @name) @def.interface
(enum_declaration name: (identifier) @name) @def.enum
(delegate_declaration name: (identifier) @name) @def.type

(method_declaration name: (identifier) @name) @def.function
(constructor_declaration name: (identifier) @name) @def.function
(property_declaration name: (identifier) @name) @def.property

(field_declaration
  (modifier) @_mod
  (variable_declaration (variable_declarator name: (identifier) @name))
  (#eq? @_mod "const")) @def.const

; FiveM/CFX handlers bound to a string, the C# twin of the Lua handler rule.
; Same in the mono (legacy) and .NET (enhanced) runtimes:
;   EventHandlers["name"] += new Action<string>((x) => { ... });
;   Exports.Add("name", new Func<int>(() => 1));
;   RegisterCommand("name", new Action<int, List<object>, string>((s, a, r) => { }), false);
; The string is the only name the lambda has, so it becomes the symbol name.
; A handler bound to a named method (`+= new Action<string>(OnStart)`) is not
; matched: the method is already a symbol. The lambda must directly follow the
; string, which keeps `query("sql", args, cb)`-style callbacks out.
(assignment_expression
  left: (element_access_expression
    subscript: (bracketed_argument_list
      . (argument (string_literal (string_literal_content) @name))))
  right: [
    (lambda_expression)
    (object_creation_expression arguments: (argument_list . (argument (lambda_expression))))
    (cast_expression value: (parenthesized_expression (lambda_expression)))
  ]) @def.handler

(invocation_expression
  arguments: (argument_list
    . (argument (string_literal (string_literal_content) @name))
    .
    (argument [
      (lambda_expression)
      (object_creation_expression arguments: (argument_list . (argument (lambda_expression))))
      (cast_expression value: (parenthesized_expression (lambda_expression)))
    ]))) @def.handler

; `using X.Y;`, `using static X.Y;`, `global using X;`, `using A = X.Y;`
(using_directive (qualified_name) @import.module) @import.import
(using_directive (identifier) @import.module .) @import.import
