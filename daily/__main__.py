"""Generate a local report and replace the previous copy only after success."""
import argparse
import fcntl
import os
from pathlib import Path
import subprocess
import sys
import tempfile


class RunError(Exception):
    """The report run failed; the previous report remains unchanged."""


def run(db, output, *, timeout=60):
    db = Path(db)
    output = Path(output)
    if not db.is_file():
        raise RunError("База не найдена или недоступна; новый отчёт не сохранён")
    if not output.parent.is_dir():
        raise RunError("Каталог отчёта не существует; новый отчёт не сохранён")
    if output.is_symlink() or output.resolve() == db.resolve():
        raise RunError("Недопустимый путь для отчёта")
    # The output directory must already be restricted by the operator. Avoid
    # following a malicious lock symlink and do not run concurrent writers.
    lock = output.parent / ("." + output.name + ".lock")
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    lock_fd = os.open(lock, flags, 0o600)
    temp_path = None
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RunError("Формирование отчёта уже выполняется") from None
        with tempfile.NamedTemporaryFile(mode="wb", prefix=".daily-", dir=output.parent, delete=False) as temp:
            temp_path = Path(temp.name)
            try:
                result = subprocess.run([sys.executable, "-m", "reports", "--db", str(db)],
                                        stdout=temp, stderr=subprocess.DEVNULL,
                                        timeout=timeout, check=False)
            except (OSError, subprocess.TimeoutExpired):
                raise RunError("Не удалось сформировать отчёт; предыдущая копия сохранена") from None
            if result.returncode != 0:
                raise RunError("Модуль отчёта завершился с ошибкой; предыдущая копия сохранена")
            if temp.tell() == 0 or temp.tell() > 10 * 1024 * 1024:
                raise RunError("Недопустимый размер отчёта; предыдущая копия сохранена")
            temp.flush()
            os.fsync(temp.fileno())
        # os.replace replaces a symlink itself, not its target. Reject one
        # anyway to avoid surprising an operator if it appeared during a run.
        if output.is_symlink():
            raise RunError("Путь отчёта изменился; предыдущая копия сохранена")
        os.replace(temp_path, output)
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        os.close(lock_fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Создать локальную сводку без отправки в сеть")
    parser.add_argument("--db", required=True, type=Path, help="существующая база SQLite")
    parser.add_argument("--output", required=True, type=Path, help="путь к локальному файлу отчёта")
    args = parser.parse_args(argv)
    try:
        run(args.db, args.output)
    except RunError as exc:
        # RunError messages are fixed by this module and contain no raw data.
        print(str(exc), file=sys.stderr)
        return 1
    except OSError:
        # Do not copy arbitrary underlying exception text (paths, URLs, data) to logs.
        print("Отчёт не обновлён. Проверьте доступ к базе и настройку службы.", file=sys.stderr)
        return 1
    print("Локальный отчёт обновлён. Отправка не выполнялась.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
