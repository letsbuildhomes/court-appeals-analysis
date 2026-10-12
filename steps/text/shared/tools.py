"""Running Poppler and Tesseract, and the character counts the text steps decide by."""
import re
import subprocess

MAX_COMMAND_SECONDS = 120


def command(args, timeout=MAX_COMMAND_SECONDS):
    try:
        result = subprocess.run(args, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f'Could not run {args[0]}: {exc}') from exc
    if result.returncode:
        detail = result.stderr.decode('utf-8', errors='replace').strip()[-1200:]
        raise RuntimeError(f'{args[0]} exited {result.returncode}: {detail}')
    return result


def version(tool, flag='-v'):
    result = command([tool, flag])
    return (result.stdout + result.stderr).decode('utf-8', errors='replace').splitlines()[0].strip()


def metrics(text):
    """(alphanumeric characters, words, bad characters) of a text."""
    alnum = sum(char.isalnum() for char in text)
    words = len(re.findall(r'\b\w+\b', text))
    bad = text.count('�') + sum(ord(char) < 32 and char not in '\n\r\t' for char in text)
    return alnum, words, bad
