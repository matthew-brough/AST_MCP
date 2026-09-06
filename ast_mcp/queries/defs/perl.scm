(package_statement name: (package) @name) @def.module
(subroutine_declaration_statement name: (bareword) @name) @def.function

(use_statement module: (package) @import.module) @import.import
