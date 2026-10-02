# LAIka v1.2 Worker Isolation Test

- **Purpose:** Verify that one worker’s state and failures do not affect another worker.
- **Isolation scenario:** Run two workers concurrently with separate jobs; have worker A mutate its state and fail while worker B continues its independent job.
- **Expected result:** Worker B completes with unchanged, correct state; worker A’s state and failure remain confined to worker A.
- **Pass/fail observation:** `PASS` if the expected isolation holds; otherwise `FAIL` with the affected worker, observed state, and relevant error.
