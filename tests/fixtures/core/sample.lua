local M = {}

--- Adds two numbers.
--- @param a number
function M.add(a, b)
  return a + b
end

--- Method style.
function M:render()
  return "w"
end

local function helper()
  return 1
end

return M
