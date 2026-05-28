from setuptools import setup, find_packages

setup(
    name="governance-logger",
    version="1.2.0",
    description="AI Governance telemetry decorator — one line to track any FastAPI route or Python function.",
    packages=find_packages(),
    python_requires=">=3.8",
    install_requires=[],          # zero hard dependencies — uses stdlib urllib as fallback
    extras_require={
        "fastapi":  ["fastapi>=0.95"],
        "async":    ["httpx>=0.24"],
        "requests": ["requests>=2.28"],
    },
)
