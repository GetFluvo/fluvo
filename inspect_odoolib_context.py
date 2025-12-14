
import inspect

import odoolib

print(f"odoolib version: {getattr(odoolib, '__version__', 'unknown')}")
print(f"odoolib file: {odoolib.__file__}")

try:
    # Use dummy credentials
    conn = odoolib.get_connection(hostname="localhost", database="db", login="admin", password="pw")
    model = conn.get_model("res.partner")
    ModelClass = type(model)
    print(f"Model Class: {ModelClass}")

    if hasattr(ModelClass, 'with_context'):
        print("HAS with_context")
        print("--- Source ---")
        try:
            print(inspect.getsource(ModelClass.with_context))
        except OSError:
            print("Could not get source (maybe compiled or built-in)")
    else:
        print("NO with_context")

    if hasattr(ModelClass, 'create'):
        print("HAS create")
    else:
        print("NO create (uses __getattr__?)")

except Exception as e:
    print(f"Error: {e}")
