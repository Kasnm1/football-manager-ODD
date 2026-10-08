#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>

#include <cstddef>
#include <cstdint>
#include <cstring>
#include <limits>
#include <vector>

namespace {
constexpr std::uint32_t kAbiVersion = 1;
constexpr int kOk = 0;
constexpr int kInvalidArgument = 1;
constexpr int kReadFailed = 2;
constexpr int kExpectedMismatch = 3;
constexpr int kWriteFailed = 4;
constexpr int kVerifyFailedRolledBack = 5;
constexpr int kVerifyFailedRollbackFailed = 6;

bool ReadExact(HANDLE process, std::uintptr_t address, void* target, std::size_t size) {
    SIZE_T read = 0;
    return ReadProcessMemory(
               process, reinterpret_cast<LPCVOID>(address), target, size, &read) != FALSE &&
           read == size;
}

bool WriteExact(HANDLE process, std::uintptr_t address, const void* source, std::size_t size) {
    SIZE_T written = 0;
    return WriteProcessMemory(
               process, reinterpret_cast<LPVOID>(address), source, size, &written) != FALSE &&
           written == size;
}
}  // namespace

extern "C" __declspec(dllexport) std::uint32_t fmodd_hook_abi_version() {
    return kAbiVersion;
}

extern "C" __declspec(dllexport) int fmodd_hook_verified_write(
    std::uintptr_t process_handle,
    std::uintptr_t address,
    const std::uint8_t* expected,
    const std::uint8_t* replacement,
    std::size_t size) {
    if (process_handle == 0 || address == 0 || expected == nullptr || replacement == nullptr ||
        size == 0 || size > 16 * 1024 * 1024) {
        return kInvalidArgument;
    }
    HANDLE process = reinterpret_cast<HANDLE>(process_handle);
    std::vector<std::uint8_t> original(size);
    if (!ReadExact(process, address, original.data(), size)) {
        return kReadFailed;
    }
    if (std::memcmp(original.data(), expected, size) != 0) {
        return kExpectedMismatch;
    }
    if (!WriteExact(process, address, replacement, size)) {
        return kWriteFailed;
    }
    std::vector<std::uint8_t> verified(size);
    const bool flush_ok = FlushInstructionCache(
                              process, reinterpret_cast<LPCVOID>(address), size) != FALSE;
    if (flush_ok && ReadExact(process, address, verified.data(), size) &&
        std::memcmp(verified.data(), replacement, size) == 0) {
        return kOk;
    }
    if (WriteExact(process, address, original.data(), size)) {
        FlushInstructionCache(process, reinterpret_cast<LPCVOID>(address), size);
        return kVerifyFailedRolledBack;
    }
    return kVerifyFailedRollbackFailed;
}

extern "C" __declspec(dllexport) int fmodd_hook_rel32(
    std::uintptr_t instruction_end,
    std::uintptr_t target,
    std::int32_t* output) {
    if (instruction_end == 0 || target == 0 || output == nullptr) {
        return kInvalidArgument;
    }
    const auto difference = static_cast<std::int64_t>(target) -
                            static_cast<std::int64_t>(instruction_end);
    if (difference < std::numeric_limits<std::int32_t>::min() ||
        difference > std::numeric_limits<std::int32_t>::max()) {
        return 2;
    }
    *output = static_cast<std::int32_t>(difference);
    return kOk;
}

extern "C" __declspec(dllexport) int fmodd_hook_pattern_matches(
    const std::uint8_t* data,
    std::size_t data_size,
    const std::uint8_t* pattern,
    const std::uint8_t* mask,
    std::size_t pattern_size,
    std::size_t* output,
    std::size_t output_capacity,
    std::size_t* total_matches) {
    if (data == nullptr || pattern == nullptr || mask == nullptr || pattern_size == 0 ||
        total_matches == nullptr || (output_capacity > 0 && output == nullptr)) {
        return kInvalidArgument;
    }
    std::size_t count = 0;
    if (data_size >= pattern_size) {
        for (std::size_t offset = 0; offset <= data_size - pattern_size; ++offset) {
            bool matched = true;
            for (std::size_t index = 0; index < pattern_size; ++index) {
                if (mask[index] != 0 && data[offset + index] != pattern[index]) {
                    matched = false;
                    break;
                }
            }
            if (matched) {
                if (count < output_capacity) {
                    output[count] = offset;
                }
                ++count;
            }
        }
    }
    *total_matches = count;
    return count > output_capacity ? 2 : kOk;
}
