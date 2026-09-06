(function_declaration name: (method_index_expression) @name) @def.method
(function_declaration name: (dot_index_expression) @name) @def.function
(function_declaration name: (identifier) @name) @def.function

(assignment_statement
  (variable_list name: (_) @name)
  (expression_list value: (function_definition))) @def.function

; Anonymous function bound to a string key. This is the FiveM/CFX idiom —
; RegisterNetEvent, AddEventHandler, RegisterCommand, RegisterNUICallback,
; exports(), and any project-local wrapper such as RegisterSecureServerEvent —
; and the general Lua callback idiom besides. The string is the only name the
; definition has, so it becomes the symbol name.
; The callee is pinned to a plain identifier and the string to the first
; argument, which keeps ordinary callbacks (`MySQL.query("q", {}, fn)`) out.
; A concatenated name (`"WDS:HB:"..GetCurrentResourceName()`) is dynamic and is
; deliberately not matched: half a literal would be a fake symbol (§V.13).
(function_call
  name: (identifier)
  arguments: (arguments
    . (string content: (string_content) @name)
    (function_definition))) @def.handler

; `local Proxy = module("vrp", "lib/Proxy")` — the vRP module loader, and
; `require "x"`. IMPORT_CALLEES discards every other callee this matches.
(function_call
  name: (identifier) @import.callee
  arguments: (arguments . (string) @import.module)) @import.require
