import subprocess
p = subprocess.Popen(["./venv/Scripts/python.exe", "-m", "uvicorn", "main:app", "--port", "8000"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
try:
    stdout, stderr = p.communicate(timeout=5)
except subprocess.TimeoutExpired:
    p.kill()
    stdout, stderr = p.communicate()
print("STDOUT:", stdout)
print("STDERR:", stderr)
