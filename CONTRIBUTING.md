# Contributing to Limiter

Thank you for your interest in contributing to Limiter!

## Getting Started

1. Fork the repository
2. Clone your fork:
   ```bash
   git clone https://github.com/YOUR_USERNAME/PG-Limiter.git
   cd PG-Limiter
   ```
3. Create a virtual environment:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   pip install -r requirements-dev.txt
   ```

## Development

### Running Locally

```bash
python3 limiter.py
```

### Running Tests

```bash
python3 -m pytest tests/ --ignore=tests/core_test.py --ignore=tests/test_minimal.py
```

Dependabot checks Python dependencies each weekday at 09:00 Asia/Tehran and GitHub
Actions and Docker images weekly on Monday at 09:00. It opens grouped minor/patch update
PRs; major updates stay separate for review. Python test tools retain their
supported major ranges in `requirements-dev.txt`.

### Code Style

- Follow PEP 8 guidelines
- Use type hints where appropriate
- Add docstrings to functions and classes

## Submitting Changes

1. Create a new branch for your feature
2. Make your changes
3. Test your changes thoroughly
4. Submit a pull request

## Reporting Issues

When reporting issues, please include:
- Python version
- Operating system
- PasarGuard panel version
- Steps to reproduce
- Error messages/logs

## License

By contributing, you agree that your contributions will be licensed under the AGPL-3.0 License.
