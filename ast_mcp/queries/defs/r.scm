(binary_operator lhs: (identifier) @name rhs: (function_definition)) @def.function

(call
  function: (identifier) @import.callee
  arguments: (arguments (argument (identifier) @import.module))) @import.import
