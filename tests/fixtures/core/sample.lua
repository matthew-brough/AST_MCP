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


local Proxy = module("vrp", "lib/Proxy")

RegisterNetEvent("sample:ping", function(payload)
  return payload
end)

-- A second listener on the same event: normal in Lua, not an ambiguity.
RegisterNetEvent("sample:ping", function()
  return helper()
end)

exports("getWidget", function()
  return M.add
end)

MySQL.query("sample/select", {}, function(rows)
  return rows
end)

RegisterNetEvent("dyn:" .. tostring(1), function()
  return nil
end)

return M
