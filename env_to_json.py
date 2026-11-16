import json

env_file = ".env"
output_file = "env.json"
env_vars = []

with open(env_file) as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, value = line.split("=", 1)
            env_vars.append({"name": key.strip(), "value": value.strip().strip('"')})

with open(output_file, "w") as f:
    json.dump(env_vars, f, indent=2)

print(f"Converted {env_file} to {output_file}")

