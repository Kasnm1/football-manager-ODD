use std::ffi::c_void;
use std::ptr;
use std::slice;

const ABI_VERSION: u32 = 1;
const WRITE_OK: i32 = 0;
const WRITE_INVALID_ARGUMENT: i32 = 1;
const WRITE_READ_FAILED: i32 = 2;
const WRITE_EXPECTED_MISMATCH: i32 = 3;
const WRITE_FAILED: i32 = 4;
const WRITE_VERIFY_FAILED_ROLLED_BACK: i32 = 5;
const WRITE_VERIFY_FAILED_ROLLBACK_FAILED: i32 = 6;

#[cfg(windows)]
#[link(name = "kernel32")]
unsafe extern "system" {
    fn ReadProcessMemory(
        process: *mut c_void,
        base_address: *const c_void,
        buffer: *mut c_void,
        size: usize,
        bytes_read: *mut usize,
    ) -> i32;
    fn WriteProcessMemory(
        process: *mut c_void,
        base_address: *mut c_void,
        buffer: *const c_void,
        size: usize,
        bytes_written: *mut usize,
    ) -> i32;
    fn FlushInstructionCache(process: *mut c_void, base_address: *const c_void, size: usize)
    -> i32;
}

#[unsafe(no_mangle)]
pub extern "C" fn fmodd_native_abi_version() -> u32 {
    ABI_VERSION
}

#[unsafe(no_mangle)]
/// # Safety
/// `raw` must contain exactly 24 readable bytes and `output` must point to
/// five writable `u64` values.
pub unsafe extern "C" fn fmodd_decode_vector_header(
    raw: *const u8,
    raw_len: usize,
    item_size: u64,
    max_count: u64,
    max_capacity_count: u64,
    allow_null_empty: u32,
    require_alignment: u32,
    output: *mut u64,
) -> i32 {
    if raw.is_null()
        || raw_len != 24
        || item_size == 0
        || allow_null_empty > 1
        || require_alignment > 1
        || output.is_null()
    {
        return 1;
    }
    let bytes = unsafe { slice::from_raw_parts(raw, raw_len) };
    let begin = u64::from_le_bytes(bytes[0..8].try_into().unwrap());
    let end = u64::from_le_bytes(bytes[8..16].try_into().unwrap());
    let capacity = u64::from_le_bytes(bytes[16..24].try_into().unwrap());
    if begin == 0 {
        if allow_null_empty == 0 || end != 0 || capacity != 0 {
            return 1;
        }
        unsafe { ptr::write_bytes(output, 0, 5) };
        return 0;
    }
    if end < begin || capacity < end {
        return 1;
    }
    let span = end - begin;
    let capacity_span = capacity - begin;
    if require_alignment != 0
        && (!span.is_multiple_of(item_size) || !capacity_span.is_multiple_of(item_size))
    {
        return 1;
    }
    let count = span / item_size;
    let capacity_count = capacity_span / item_size;
    if (max_count > 0 && count > max_count)
        || (max_capacity_count > 0 && capacity_count > max_capacity_count)
    {
        return 1;
    }
    let values = [begin, end, capacity, count, capacity_count];
    unsafe { ptr::copy_nonoverlapping(values.as_ptr(), output, values.len()) };
    0
}

fn poisson_value(k: usize, mean: f64) -> f64 {
    let mut factorial = 1.0;
    for value in 2..=k {
        factorial *= value as f64;
    }
    (-mean).exp() * mean.powi(k as i32) / factorial
}

#[unsafe(no_mangle)]
/// # Safety
/// `output` must point to writable memory for one `f64`.
pub unsafe extern "C" fn fmodd_poisson(k: i32, mean: f64, output: *mut f64) -> i32 {
    if k < 0 || !mean.is_finite() || output.is_null() {
        return 1;
    }
    unsafe { *output = poisson_value(k as usize, mean) };
    0
}

#[unsafe(no_mangle)]
/// # Safety
/// `output` must point to at least `output_len` writable `f64` values.
pub unsafe extern "C" fn fmodd_score_matrix(
    home_xg: f64,
    away_xg: f64,
    maximum: usize,
    rho: f64,
    output: *mut f64,
    output_len: usize,
) -> i32 {
    if !home_xg.is_finite()
        || !away_xg.is_finite()
        || !rho.is_finite()
        || !(1..=64).contains(&maximum)
        || output.is_null()
    {
        return 1;
    }
    let side = match maximum.checked_add(1) {
        Some(value) => value,
        None => return 1,
    };
    let required = match side.checked_mul(side) {
        Some(value) => value,
        None => return 1,
    };
    if output_len < required {
        return 2;
    }
    let matrix = unsafe { slice::from_raw_parts_mut(output, required) };
    for home in 0..side {
        for away in 0..side {
            let probability = poisson_value(home, home_xg) * poisson_value(away, away_xg);
            matrix[home * side + away] = probability;
        }
    }
    let correction = [
        (0usize, 0usize, 1.0 - home_xg * away_xg * rho),
        (0, 1, 1.0 + home_xg * rho),
        (1, 0, 1.0 + away_xg * rho),
        (1, 1, 1.0 - rho),
    ];
    for (home, away, factor) in correction {
        let index = home * side + away;
        matrix[index] *= factor.max(0.01);
    }
    let total: f64 = matrix.iter().sum();
    if !total.is_finite() || total <= 0.0 {
        return 3;
    }
    for probability in matrix {
        *probability /= total;
    }
    0
}

fn quarter_lines(line: f64) -> ([f64; 2], usize) {
    if (line * 4.0).abs() % 2.0 == 1.0 {
        ([line - 0.25, line + 0.25], 2)
    } else {
        ([line, 0.0], 1)
    }
}

#[unsafe(no_mangle)]
/// # Safety
/// `matrix` must contain `rows * columns` readable `f64` values and `output`
/// must point to three writable `f64` values.
pub unsafe extern "C" fn fmodd_market_weights(
    matrix: *const f64,
    rows: usize,
    columns: usize,
    line: f64,
    market: u32,
    side: u32,
    over: u32,
    output: *mut f64,
) -> i32 {
    if matrix.is_null()
        || output.is_null()
        || rows == 0
        || columns == 0
        || rows.checked_mul(columns).is_none()
        || !line.is_finite()
        || market > 2
        || side > 1
        || over > 1
    {
        return 1;
    }
    let values = unsafe { slice::from_raw_parts(matrix, rows * columns) };
    if values.iter().any(|value| !value.is_finite()) {
        return 1;
    }
    let (lines, line_count) = quarter_lines(line);
    let mut result = [0.0f64; 3];
    for actual_line in lines.iter().take(line_count) {
        let mut local = [0.0f64; 3];
        for home in 0..rows {
            for away in 0..columns {
                let probability = values[home * columns + away];
                let mut value = match market {
                    0 if side == 0 => home as f64 - away as f64 + actual_line,
                    0 => away as f64 - home as f64 - actual_line,
                    1 => home as f64 + away as f64 - actual_line,
                    2 if side == 0 => home as f64 - actual_line,
                    2 => away as f64 - actual_line,
                    _ => unreachable!(),
                };
                if market != 0 && over == 0 {
                    value = -value;
                }
                if value > 0.0 {
                    local[0] += probability;
                } else if value < 0.0 {
                    local[2] += probability;
                } else {
                    local[1] += probability;
                }
            }
        }
        for index in 0..3 {
            result[index] += local[index] / line_count as f64;
        }
    }
    unsafe { ptr::copy_nonoverlapping(result.as_ptr(), output, result.len()) };
    0
}

#[cfg(windows)]
unsafe fn read_exact(handle: *mut c_void, address: usize, target: &mut [u8]) -> bool {
    let mut read = 0usize;
    unsafe {
        ReadProcessMemory(
            handle,
            address as *const c_void,
            target.as_mut_ptr().cast(),
            target.len(),
            &mut read,
        ) != 0
            && read == target.len()
    }
}

#[cfg(windows)]
unsafe fn write_exact(handle: *mut c_void, address: usize, source: &[u8]) -> bool {
    let mut written = 0usize;
    unsafe {
        WriteProcessMemory(
            handle,
            address as *mut c_void,
            source.as_ptr().cast(),
            source.len(),
            &mut written,
        ) != 0
            && written == source.len()
    }
}

#[unsafe(no_mangle)]
#[cfg(windows)]
/// # Safety
/// `expected` and `replacement` must each contain `size` readable bytes. The
/// process handle and address must remain valid for the complete transaction.
pub unsafe extern "C" fn fmodd_verified_write(
    process_handle: usize,
    address: usize,
    expected: *const u8,
    replacement: *const u8,
    size: usize,
    flush_instruction_cache: u32,
) -> i32 {
    if process_handle == 0
        || address == 0
        || expected.is_null()
        || replacement.is_null()
        || size == 0
        || size > 16 * 1024 * 1024
        || flush_instruction_cache > 1
    {
        return WRITE_INVALID_ARGUMENT;
    }
    let handle = process_handle as *mut c_void;
    let expected = unsafe { slice::from_raw_parts(expected, size) };
    let replacement = unsafe { slice::from_raw_parts(replacement, size) };
    let mut original = vec![0u8; size];
    if !unsafe { read_exact(handle, address, &mut original) } {
        return WRITE_READ_FAILED;
    }
    if original != expected {
        return WRITE_EXPECTED_MISMATCH;
    }
    if !unsafe { write_exact(handle, address, replacement) } {
        return WRITE_FAILED;
    }
    let mut verified = vec![0u8; size];
    let flush_ok = flush_instruction_cache == 0
        || unsafe { FlushInstructionCache(handle, address as *const c_void, size) } != 0;
    if flush_ok && unsafe { read_exact(handle, address, &mut verified) } && verified == replacement
    {
        return WRITE_OK;
    }
    if unsafe { write_exact(handle, address, &original) } {
        if flush_instruction_cache != 0 {
            unsafe { FlushInstructionCache(handle, address as *const c_void, size) };
        }
        return WRITE_VERIFY_FAILED_ROLLED_BACK;
    }
    WRITE_VERIFY_FAILED_ROLLBACK_FAILED
}
