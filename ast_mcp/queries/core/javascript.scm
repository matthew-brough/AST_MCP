(function_declaration name: (identifier) @name) @def.function
(generator_function_declaration name: (identifier) @name) @def.function
(class_declaration name: (identifier) @name) @def.class
(method_definition name: (property_identifier) @name) @def.method

(variable_declarator name: (identifier) @name value: (arrow_function)) @def.function
(variable_declarator name: (identifier) @name value: (function_expression)) @def.function

(import_statement) @import.import
(call_expression
  function: (identifier) @import.callee
  arguments: (arguments (string) @import.module)) @import.require
