(function_declaration name: (method_index_expression) @name) @def.method
(function_declaration name: (dot_index_expression) @name) @def.function
(function_declaration name: (identifier) @name) @def.function

(assignment_statement
  (variable_list name: (_) @name)
  (expression_list value: (function_definition))) @def.function
