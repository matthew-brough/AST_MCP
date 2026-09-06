(function_declaration name: (identifier) @name) @def.function
(generator_function_declaration name: (identifier) @name) @def.function
(function_signature name: (identifier) @name) @def.function
(class_declaration name: (type_identifier) @name) @def.class
(abstract_class_declaration name: (type_identifier) @name) @def.class
(interface_declaration name: (type_identifier) @name) @def.interface
(type_alias_declaration name: (type_identifier) @name) @def.type
(enum_declaration name: (identifier) @name) @def.type
(method_definition name: (property_identifier) @name) @def.method
(method_signature name: (property_identifier) @name) @def.method

(variable_declarator name: (identifier) @name value: (arrow_function)) @def.function
(variable_declarator name: (identifier) @name value: (function_expression)) @def.function

(import_statement) @import.import
(call_expression
  function: (identifier) @import.callee
  arguments: (arguments (string) @import.module)) @import.require
