(mixin_statement name: (identifier) @name) @def.function
(function_statement name: (identifier) @name) @def.function
(rule_set (selectors) @name) @def.rule
(keyframes_statement (keyframes_name) @name) @def.rule

(import_statement (string_value) @import.module) @import.import
