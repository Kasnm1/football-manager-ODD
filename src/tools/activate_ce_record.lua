local outputRoot = CE_INSPECTOR_OUTPUT_ROOT or [[U:\Work\FM\data\research\ce_record_activation]]
local expectedProcessId = tonumber(CE_EXPECTED_PROCESS_ID or 0)
local recordId = tonumber(CE_RECORD_ID or -1)

local function writeResult(lines)
  local output = assert(io.open(outputRoot .. [[\activation_result.txt]], "w"))
  output:write(table.concat(lines, "\n") .. "\n")
  output:close()
end

local function run()
  local processId = getOpenedProcessID()
  if processId == 0 and expectedProcessId > 0 then
    openProcess(expectedProcessId)
    sleep(500)
    processId = getOpenedProcessID()
  end
  if processId ~= expectedProcessId then
    error(string.format("CE target PID mismatch: expected %d, got %d", expectedProcessId, processId))
  end

  local record = assert(
    getAddressList().getMemoryRecordByID(recordId),
    string.format("memory record %d was not found", recordId)
  )
  local before = record.Active
  record.Active = true
  local after = record.Active
  writeResult({
    "process_id=" .. tostring(processId),
    "record_id=" .. tostring(recordId),
    "description=" .. tostring(record.Description or ""),
    "active_before=" .. tostring(before),
    "active_after=" .. tostring(after),
  })
end

local ok, message = xpcall(run, debug.traceback)
if not ok then
  writeResult({"error=" .. tostring(message):gsub("[\r\n]+", " | ")})
end
