# %% Hello
message = "Hello from Neura"
print(message)

# %% Persistent state
# Run this cell several times, then restart the runtime and run it again.
counter = globals().get("counter", 0) + 1
print(f"Run number: {counter}")
counter

# %% Streams
import sys
print("This goes to stdout")
print("This goes to stderr", file=sys.stderr)

# %% Error capture
# The error appears in Neura Outputs, without killing the session.
print("Output before the error is preserved")
raise ValueError("An intentional demo error")

# %% Interrupt
# Run this cell, then click Interrupt in Neura Outputs.
import time
print("Sleeping for 20 seconds — try Interrupt or Restart runtime")
time.sleep(20)
print("Finished sleeping")

# %% Rich outputs
# A DataFrame result renders as a table, HTML as a sanitized block, and a
# matplotlib figure as a PNG in Neura Outputs.
import pandas as pd

demo = pd.DataFrame(
    {
        "day": ["Mon", "Tue", "Wed", "Thu", "Fri"],
        "visits": [120, 145, 132, 178, 190],
        "signups": [12, 15, 11, 21, 24],
    }
)
demo

# %% Plot
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

plt.figure(figsize=(4, 2.5))
plt.plot(demo["day"], demo["visits"], marker="o")
plt.title("Weekly visits")
plt.show()

# %% HTML output
from IPython.display import HTML, display

display(HTML("<table><tr><th>Metric</th><th>Value</th></tr><tr><td>Rows</td><td>5</td></tr></table>"))

# %% [markdown] Notes
# The commands execute code in the editor buffer, including unsaved changes.
# A non-empty selection executes exactly that text; an empty selection runs the current line.
# Rich outputs, DataFrame previews and plots are captured per cell; the notebook
# bridge (Neura: Attach to Notebook Kernel) runs the same cells inside a live
# Jupyter kernel instead of a managed process.
