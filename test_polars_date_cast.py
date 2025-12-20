"""Test to verify polars casting behavior with date strings."""

import polars as pl

# Simulate what we get from Odoo
data = {
    "id": [1, 2, 3],
    "date_order": ["2025-12-08 10:13:12", "2025-12-08 09:56:34", "2025-12-08 11:01:32"],
}

df = pl.DataFrame(data)
print("=== Original DataFrame ===")
print(df)
print(f"\nSchema: {df.schema}")
print()

# Try to cast directly to Datetime (this is what the library does)
schema = {"id": pl.Int64, "date_order": pl.Datetime}
try:
    casted_df = df.cast(schema, strict=False)
    print("=== After casting with strict=False ===")
    print(casted_df)
    print(f"\nSchema: {casted_df.schema}")
    print()
except Exception as e:
    print(f"Error: {e}")

# The correct way: parse the string first
print("=== Correct approach: parse datetime string first ===")
df_correct = df.with_columns(
    [pl.col("date_order").str.to_datetime("%Y-%m-%d %H:%M:%S")]
)
print(df_correct)
print(f"\nSchema: {df_correct.schema}")
