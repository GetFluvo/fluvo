# Auto-Scaling Batch Size Feature for odoo-data-flow

## Overview
Implement an auto-scaling mechanism that detects when batches are failing due to timeouts or other performance issues and automatically reduces the batch size for subsequent attempts, then gradually scales back up when conditions improve.

## Problem Statement
Currently, odoo-data-flow uses a fixed batch size throughout the import process. When batches are too large for the server capacity or when complex records cause timeouts, the entire batch fails. The `--fail` option reprocesses these failures with the same parameters, often resulting in continued failures.

## Solution Design

### Core Algorithm

```
initial_batch_size = user_specified_size
current_batch_size = initial_batch_size
consecutive_successes = 0
scale_up_threshold = 10  # Number of consecutive successful batches before attempting to scale up
min_batch_size = 1       # Minimum allowed batch size
scaling_factor = 0.5     # Reduce batch size by 50% on failure
```

### Behavior Logic

1. **Normal Operation**: Process batches using `current_batch_size`
2. **Failure Detection**: When a batch fails due to timeout or connection-related errors:
   - Reduce `current_batch_size` by the scaling factor (50%)
   - Reset `consecutive_successes` counter to 0
   - Continue with the smaller batch size
3. **Success Tracking**: When a batch succeeds:
   - Increment `consecutive_successes`
   - If `consecutive_successes >= scale_up_threshold` and `current_batch_size < initial_batch_size`:
     - Try to scale up: `current_batch_size = min(current_batch_size * 1.5, initial_batch_size)`
4. **Error Types to Detect**: 
   - Network timeout errors
   - "IndexError: tuple index out of range" (server-side timeout)
   - HTTP timeout errors
   - Connection reset errors
   - Any exception indicating server overload

### Implementation Details

#### Module to Modify
- `odoo_data_flow/lib/odoo_lib.py` or the main import logic module
- Potentially `odoo_data_flow/lib/internal/rpc_thread.py` for threading logic

#### New Configuration Options
Add to the existing command line interface:
- `--auto-scaling`: Enable/disable the auto-scaling feature (default: false)
- `--min-batch-size INTEGER`: Minimum allowed batch size (default: 1)

#### New Command Line Options
```bash
--auto-scaling              Enable automatic batch size scaling based on success/failure
--min-batch-size INTEGER    Minimum batch size when auto-scaling (default: 1)
```

### Auto-Scaling Logic Flow

```
function process_with_auto_scaling(file_data, model, batch_size, options):
    if not options.auto_scaling:
        return standard_import(file_data, model, batch_size, options)
    
    initial_batch_size = batch_size
    current_batch_size = batch_size
    consecutive_successes = 0
    failed_batches = {}  # Track which specific batches failed
    
    for batch in create_batches(file_data, current_batch_size):
        success = attempt_batch(batch, model, current_batch_size, options)
        
        if success:
            consecutive_successes += 1
            # Attempt scale up after sustained success
            if (consecutive_successes >= scale_up_threshold 
                and current_batch_size < initial_batch_size):
                new_batch_size = min(int(current_batch_size * 1.5), initial_batch_size)
                log(f"Scaling up batch size from {current_batch_size} to {new_batch_size}")
                current_batch_size = new_batch_size
        else:
            # Scale down on failure
            consecutive_successes = 0
            new_batch_size = max(int(current_batch_size * scaling_factor), options.min_batch_size)
            if new_batch_size != current_batch_size:
                log(f"Scaling down batch size from {current_batch_size} to {new_batch_size} due to failure")
                current_batch_size = new_batch_size
            
            # Handle the failed batch (retry with new size or add to failed_batches)
            failed_batches[batch.id] = {
                'data': batch,
                'original_size': current_batch_size,
                'attempts': 1
            }
    
    return failed_batches
```

### Error Detection

The system should specifically look for these error patterns:
- `IndexError: tuple index out of range` (from the Odoo server API)
- `requests.exceptions.Timeout`
- `socket.timeout`
- `ConnectionResetError`
- `requests.exceptions.ConnectionError`
- Any error that contains phrases like "timeout", "connection", "reset"

### Gradual Scale-Up Logic

When scaling up, use conservative increases (e.g., 50% increase) to avoid immediately triggering another failure. Only attempt to scale up when:
1. There have been sufficient consecutive successes (e.g., 10 batches)
2. The current batch size is below the initial size
3. The server appears stable

### Testing Considerations

The implementation should include tests for:
- Normal operation without auto-scaling (should behave identically)
- Auto-scaling triggered by simulated failures
- Recovery and scale-up after stability returns
- Edge cases (already at minimum batch size, etc.)

## Benefits

1. **Improved Success Rate**: Automatically adapts to server conditions
2. **Better Performance**: Maintains larger batch sizes when possible
3. **Reduced Manual Intervention**: Less need to manually adjust batch sizes
4. **Server-Friendly**: Adjusts to server capacity automatically

## Backward Compatibility

- Default behavior remains unchanged (auto-scaling disabled)
- Existing scripts will continue to work without modification
- Only when `--auto-scaling` is explicitly enabled does the new behavior take effect