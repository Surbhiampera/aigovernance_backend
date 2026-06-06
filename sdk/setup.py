from setuptools import setup, find_packages

setup(
    name="governance-sdk",
    version="0.1.0",
    description="AI Governance telemetry SDK — capture, govern, and observe LLM usage in real time.",
    packages=find_packages(),
    python_requires=">=3.10",
    install_requires=["requests>=2.28"],
    extras_require={
        "openai": ["openai>=1.0"],
        "anthropic": ["anthropic>=0.25"],
    },
)
