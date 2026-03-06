#!/bin/bash
echo "Setting up Antigravity Global Environment..."

# 1. Create Python Virtual Environment (venv)
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
else
    echo "Virtual environment already exists."
fi

# 2. Activate venv
source venv/bin/activate

# 3. Upgrade pip and install testing/linting requirements
pip install --upgrade pip
pip install -r requirements.txt

echo "Setup Complete! To activate the environment, run:"
echo "source venv/bin/activate"
