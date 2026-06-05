
import subprocess
import sys

# Path to your bash script
bash_script_path = "./build_randomx.sh"  # Change this to your script path

# Optional: Arguments to pass to the bash script
args = ["arg1", "arg2"]  # Replace with your arguments or leave as []

try:
    # Run the bash script using subprocess.run
    result = subprocess.run(
        ["bash", bash_script_path] + args,  # Command and arguments
        capture_output=True,                # Capture stdout and stderr
        text=True,                          # Return output as string instead of bytes
        check=True                          # Raise CalledProcessError on non-zero exit
    )

    # Print the output from the bash script
    print("Output:
", result.stdout)
    print("Errors (if any):
", result.stderr)

except subprocess.CalledProcessError as e:
    print(f"Error: Bash script exited with code {e.returncode}")
    print("Output:
", e.output)
    print("Errors:
", e.stderr)

except FileNotFoundError:
    print(f"Error: The file {bash_script_path} does not exist")

except Exception as e:
    print(f"An unexpected error occurred: {e}")

# Usage Example:
# Save this file as run_bash.py and run:
# python run_bash.py
