(function_definition name: (word) @name) @def.function
(declaration_command (variable_assignment name: (variable_name) @name)) @def.var

(command
  name: (command_name) @import.callee
  argument: (word) @import.module) @import.import
