import hashlib
import os
import shutil
import subprocess
import tempfile
import threading
import time
import warnings
import zipfile
import zlib
from pathlib import Path
from disklru import DiskLRUCache  # type: ignore
from dataclasses import dataclass
from tempfile import NamedTemporaryFile, TemporaryDirectory
from threading import Timer
from typing import List, Callable
import re


from fastapi import (
    BackgroundTasks,
    FastAPI,
    File,
    Header,  # type: ignore
    HTTPException,
    UploadFile,
)
from fastapi.responses import FileResponse, RedirectResponse  # type: ignore
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

_VOLUME_MAPPED_SRC = Path("/host/fastled/src")
_RSYNC_DEST = Path("/js/fastled/src")

_TEST = False
_UPLOAD_LIMIT = 10 * 1024 * 1024
# Protect the endpoints from random bots.
# Note that that the wasm_compiler.py greps for this string to get the URL of the server.
# Changing the name could break the compiler.
_AUTH_TOKEN = "oBOT5jbsO4ztgrpNsQwlmFLIKB"

_SOURCE_EXTENSIONS = [".cpp", ".hpp", ".h", ".ino"]

_LIVE_GIT_UPDATES_INTERVAL = 600  # Fetch the git repository every 10 mins.
_ALLOW_SHUTDOWN = os.environ.get("ALLOW_SHUTDOWN", "false").lower() in ["true", "1"]
_NO_SKETCH_CACHE = os.environ.get("NO_SKETCH_CACHE", "false").lower() in ["true", "1"]
_LIVE_GIT_FASTLED_DIR = Path("/git/fastled2")

_NO_AUTO_UPDATE = (
    os.environ.get("NO_AUTO_UPDATE", "0") in ["1", "true"]
    or _VOLUME_MAPPED_SRC.exists()
) and False
_LIVE_GIT_UPDATES_ENABLED = not _NO_AUTO_UPDATE
_START_TIME = time.time()


if _NO_SKETCH_CACHE:
    print("Sketch caching disabled")

upload_dir = Path("/uploads")
upload_dir.mkdir(exist_ok=True)
compile_lock = threading.Lock()

output_dir = Path("/output")
output_dir.mkdir(exist_ok=True)

# Initialize disk cache
CACHE_FILE = output_dir / "compile_cache.db"
CACHE_MAX_ENTRIES = 50
disk_cache = DiskLRUCache(str(CACHE_FILE), CACHE_MAX_ENTRIES)
app = FastAPI()


@dataclass
class SrcFileHashResult:
    hash: str
    stdout: str
    error: bool


class UploadSizeMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, max_upload_size: int):
        super().__init__(app)
        self.max_upload_size = max_upload_size

    async def dispatch(self, request: Request, call_next):
        if request.method == "POST" and "/compile/wasm" in request.url.path:
            print(
                f"Upload request with content-length: {request.headers.get('content-length')}"
            )
            content_length = request.headers.get("content-length")
            if content_length:
                content_length = int(content_length)  # type: ignore
                if content_length > self.max_upload_size:  # type: ignore
                    return Response(
                        status_code=413,
                        content=f"File size exceeds {self.max_upload_size} byte limit",
                    )
        return await call_next(request)


app.add_middleware(UploadSizeMiddleware, max_upload_size=_UPLOAD_LIMIT)


def hash_string(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def update_live_git_repo() -> None:
    if not _LIVE_GIT_UPDATES_ENABLED:
        return
    try:
        if not _LIVE_GIT_FASTLED_DIR.exists():
            subprocess.run(
                [
                    "git",
                    "clone",
                    "https://github.com/fastled/fastled.git",
                    "/git/fastled2",
                ],
                check=True,
            )
            print("Cloned live FastLED repository")
        else:
            print("Updating live FastLED repository")
            subprocess.run(
                ["git", "fetch", "origin"],
                check=True,
                capture_output=True,
                cwd=_LIVE_GIT_FASTLED_DIR,
            )
            subprocess.run(
                ["git", "reset", "--hard", "origin/master"],
                check=True,
                capture_output=True,
                cwd=_LIVE_GIT_FASTLED_DIR,
            )
            print("Live FastLED repository updated successfully")
    except subprocess.CalledProcessError as e:
        warnings.warn(
            f"Error updating live FastLED repository: {e.stdout}\n\n{e.stderr}"
        )


def try_get_cached_zip(hash: str) -> bytes | None:
    if _NO_SKETCH_CACHE:
        print("Sketch caching disabled, skipping cache get")
        return None
    return disk_cache.get_bytes(hash)


def cache_put(hash: str, data: bytes) -> None:
    if _NO_SKETCH_CACHE:
        print("Sketch caching disabled, skipping cache put")
        return
    disk_cache.put_bytes(hash, data)


def sync_src_to_target(
    src: Path, dst: Path, callback: Callable[[], None] | None = None
) -> bool:
    """Sync the volume mapped source directory to the FastLED source directory."""
    suppress_print = _START_TIME + 30 > time.time()  # Don't print during initial volume map.
    if not src.exists():
        # Volume is not mapped in so we don't rsync it.
        print(f"Skipping rsync, as fastled src at {src} doesn't exist")
        return False
    try:
        print("\nSyncing source directories...")
        with compile_lock:
            # Use rsync to copy files, preserving timestamps and deleting removed files
            cp: subprocess.CompletedProcess = subprocess.run(
                ["rsync", "-av", "--info=NAME", "--delete", f"{src}/", f"{dst}/"],
                check=True,
                text=True,
                capture_output=True,
            )
            if cp.returncode == 0:
                changed = False
                changed_lines: list[str] = []
                lines = cp.stdout.split("\n")
                for line in lines:
                    suffix = line.strip().split(".")[-1]
                    if suffix in ["cpp", "h", "hpp", "ino", "py", "js", "html", "css"]:
                        if not suppress_print:
                            print(f"Changed file: {line}")
                        changed = True
                        changed_lines.append(line)
                if changed:
                    if not suppress_print:
                        print(f"FastLED code had updates: {changed_lines}")
                    if callback:
                        callback()
                    return True
                print("Source directory synced successfully with no changes")
                return False
            else:
                print(f"Error syncing directories: {cp.stdout}\n\n{cp.stderr}")
                return False

    except subprocess.CalledProcessError as e:
        print(f"Error syncing directories: {e.stdout}\n\n{e.stderr}")
    except Exception as e:
        print(f"Error syncing directories: {e}")
    return False


def sync_source_directory_if_volume_is_mapped(
    callback: Callable[[], None] | None = None
) -> bool:
    """Sync the volume mapped source directory to the FastLED source directory."""
    if not _VOLUME_MAPPED_SRC.exists():
        # Volume is not mapped in so we don't rsync it.
        print("Skipping rsync, as fastled src volume not mapped")
        return False
    return sync_src_to_target(_VOLUME_MAPPED_SRC, _RSYNC_DEST, callback=callback)


def sync_live_git_to_target() -> None:
    if not _LIVE_GIT_UPDATES_ENABLED:
        return
    update_live_git_repo()  # no lock

    def on_files_changed() -> None:
        print("FastLED source changed from github repo, clearing disk cache.")
        disk_cache.clear()
    sync_src_to_target(
        _LIVE_GIT_FASTLED_DIR, _RSYNC_DEST, callback=on_files_changed
    )
    Timer(
        _LIVE_GIT_UPDATES_INTERVAL, sync_live_git_to_target
    ).start()  # Start the periodic git update


@dataclass
class ProjectFiles:
    """A class to represent the project files."""

    src_files: list[Path]
    other_files: list[Path]


def collect_files(directory: Path) -> ProjectFiles:
    """Collect files from a directory and separate them into source and other files.

    Args:
        directory (Path): The directory to scan for files.

    Returns:
        ProjectFiles: Object containing lists of source and other files.
    """
    print(f"Collecting files from {directory}")
    src_files: list[Path] = []
    other_files: list[Path] = []

    def is_source_file(filename: str) -> bool:
        return any(filename.endswith(ext) for ext in _SOURCE_EXTENSIONS)

    for root, _, filenames in os.walk(str(directory)):
        for filename in filenames:
            print(f"Checking file: {filename}")
            file_path = Path(os.path.join(root, filename))

            if is_source_file(filename):
                src_files.append(file_path)
            else:
                other_files.append(file_path)

    return ProjectFiles(src_files=src_files, other_files=other_files)


def concatenate_files(file_list: List[Path], output_file: Path) -> None:
    """Concatenate files into a single output file.

    Args:
        file_list (List[str]): List of file paths to concatenate.
        output_file (str): Path to the output file.
    """
    with open(str(output_file), "w", encoding="utf-8") as outfile:
        for file_path in file_list:
            outfile.write(f"// File: {file_path}\n")
            with open(file_path, "r", encoding="utf-8") as infile:
                outfile.write(infile.read())
                outfile.write("\n\n")


def collapse_spaces_preserve_cstrings(line: str):
    def replace_outside_cstrings(match):
        # This function processes the part outside of C strings
        content = match.group(0)
        if content.startswith('"') or content.startswith("'"):
            return content  # It's inside a C string, keep as is
        else:
            # Collapse spaces outside of C strings
            return " ".join(content.split())

    # Regular expression to match C strings and non-C string parts
    pattern = r'\"(?:\\.|[^\"])*\"|\'.*?\'|[^"\']+'
    processed_line = "".join(
        replace_outside_cstrings(match) for match in re.finditer(pattern, line)
    )
    return processed_line


# return a hash
def preprocess_with_gcc(input_file: Path, output_file: Path) -> None:
    """Preprocess a file with GCC, leaving #include directives intact.

    Args:
        input_file (str): Path to the input file.
        output_file (str): Path to the preprocessed output file.
    """
    # Convert paths to absolute paths
    # input_file = os.path.abspath(str(input_file))
    input_file = input_file.absolute()
    output_file = output_file.absolute()
    temp_input = str(input_file) + ".tmp"

    try:
        # Create modified version of input that comments out includes
        with open(str(input_file), "r") as fin, open(str(temp_input), "w") as fout:
            for line in fin:
                if line.strip().startswith("#include"):
                    fout.write(f"// PRESERVED: {line}")
                else:
                    fout.write(line)

        # Run GCC preprocessor with explicit output path in order to remove
        # comments. This is necessary to ensure that the hash
        # of the preprocessed file is consistent without respect to formatting
        # and whitespace.
        gcc_command: list[str] = [
            "gcc",
            "-E",  # Preprocess only
            "-P",  # No line markers
            "-fdirectives-only",
            "-fpreprocessed",  # Handle preprocessed input
            "-x",
            "c++",  # Explicitly treat input as C++ source
            "-o",
            str(output_file),  # Explicit output file
            temp_input,
        ]

        result = subprocess.run(gcc_command, check=True, capture_output=True, text=True)

        if not os.path.exists(output_file):
            raise FileNotFoundError(
                f"GCC failed to create output file. stderr: {result.stderr}"
            )

        # Restore include lines
        with open(output_file, "r") as f:
            content = f.read()

        content = content.replace("// PRESERVED: #include", "#include")
        out_lines: list[str] = []
        # now preform minification to further strip out horizontal whitespace and // File: comments.
        for line in content.split("\n"):
            # Skip file marker comments and empty lines
            line = line.strip()
            if not line:  # skip empty line
                continue
            if line.startswith(
                "// File:"
            ):  # these change because of the temp file, so need to be removed.
                continue
            # Collapse multiple spaces into single space and strip whitespace
            # line = ' '.join(line.split())
            line = collapse_spaces_preserve_cstrings(line)
            out_lines.append(line)
        # Join with new lines
        content = "\n".join(out_lines)
        with open(output_file, "w") as f:
            f.write(content)

        print(f"Preprocessed file saved to {output_file}")

    except subprocess.CalledProcessError as e:
        print(f"GCC preprocessing failed: {e.stderr}")
        raise
    except Exception as e:
        print(f"Preprocessing error: {str(e)}")
        raise
    finally:
        # Clean up temporary file
        try:
            if os.path.exists(temp_input):
                os.remove(temp_input)
        except:  # noqa: E722
            warnings.warn(f"Failed to remove temporary file: {temp_input}")
            pass


def generate_hash_of_src_files(src_files: list[Path]) -> SrcFileHashResult:
    """Generate a hash of all source files in a directory.

    Args:
        src_files (list[Path]): List of source files to hash.

    Returns:
        SrcFileHashResult: Object containing hash, stdout and error status.
    """
    try:
        with TemporaryDirectory() as temp_dir:
            temp_file = Path(temp_dir) / "concatenated_output.cpp"
            preprocessed_file = Path(temp_dir) / "preprocessed_output.cpp"
            concatenate_files(src_files, Path(temp_file))
            preprocess_with_gcc(temp_file, preprocessed_file)
            contents = preprocessed_file.read_text()

            # strip the last line in it:
            parts = contents.split("\n")
            out_lines: list[str] = []
            for line in parts:
                if "concatenated_output.cpp" not in line:
                    out_lines.append(line)

            contents = "\n".join(out_lines)
            return SrcFileHashResult(
                hash=hash_string(contents),
                stdout="",  # No stdout in success case
                error=False,
            )
    except Exception:
        import traceback

        stack_trace = traceback.format_exc()
        print(stack_trace)
        return SrcFileHashResult(hash="", stdout=stack_trace, error=True)


def generate_hash_of_project_files(root_dir: Path) -> str:
    """Generate a hash of all files in a directory.

    Args:
        root_dir (Path): The root directory to hash.

    Returns:
        str: The hash of all files in the directory.
    """
    project_files = collect_files(root_dir)
    src_result = generate_hash_of_src_files(project_files.src_files)
    if src_result.error:
        raise Exception(f"Error hashing source files: {src_result.stdout}")

    other_files = project_files.other_files
    # for all other files, don't pre-process them, just hash them
    hash_object = hashlib.sha256()
    for file in other_files:
        hash_object.update(file.read_bytes())
    other_files_hash = hash_object.hexdigest()
    return hash_string(src_result.hash + other_files_hash)


def compile_source(
    temp_src_dir: Path,
    file_path: Path,
    background_tasks: BackgroundTasks,
    build_mode: str,
    profile: bool,
    hash_value: str | None = None,
) -> FileResponse | HTTPException:
    """Compile source code and return compiled artifacts as a zip file."""
    temp_zip_dir = None
    try:
        # Find the first directory in temp_src_dir
        src_dir = next(Path(temp_src_dir).iterdir())
        print(f"\nFound source directory: {src_dir}")
    except StopIteration:
        return HTTPException(
            status_code=500,
            detail=f"No files found in extracted directory: {temp_src_dir}",
        )

    print("Files are ready, waiting for compile lock...")
    compile_lock_start = time.time()
    with compile_lock:
        compile_lock_end = time.time()

        print("\nRunning compiler...")
        cmd = [
            "python",
            "run.py",
            "compile",
            f"--mapped-dir={temp_src_dir}",
        ]
        cmd.append(f"--{build_mode.lower()}")
        if profile:
            cmd.append("--profile")
        cp = subprocess.run(cmd, cwd="/js", capture_output=True, text=True)
        stdout = cp.stdout
        return_code = cp.returncode
        if return_code != 0:
            return HTTPException(
                status_code=400,
                detail=f"Compilation failed with return code {return_code}:\n{stdout}",
            )
    compile_time = time.time() - compile_lock_end
    compile_lock_time = compile_lock_end - compile_lock_start

    print(f"\nCompiler output:\nstdout:\n{stdout}")
    print(f"Compile lock time: {compile_lock_time:.2f}s")
    print(f"Compile time: {compile_time:.2f}s")

    # Find the fastled_js directory
    fastled_js_dir = src_dir / "fastled_js"
    print(f"\nLooking for fastled_js directory at: {fastled_js_dir}")

    if not fastled_js_dir.exists():
        print(f"Directory contents of {src_dir}:")
        for path in src_dir.rglob("*"):
            print(f"  {path}")
        return HTTPException(
            status_code=500,
            detail=f"Compilation artifacts not found at {fastled_js_dir}",
        )

    # Replace separate stdout/stderr files with single out.txt
    out_txt = fastled_js_dir / "out.txt"
    perf_txt = fastled_js_dir / "perf.txt"
    hash_txt = fastled_js_dir / "hash.txt"
    print(f"\nSaving combined output to: {out_txt}")
    out_txt.write_text(stdout)
    perf_txt.write_text(
        f"Compile lock time: {compile_lock_time:.2f}s\nCompile time: {compile_time:.2f}s"
    )
    if hash_value is not None:
        hash_txt.write_text(hash_value)

    output_dir.mkdir(exist_ok=True)  # Ensure output directory exists
    output_zip_path = output_dir / f"fastled_output_{hash(str(file_path))}.zip"
    print(f"\nCreating output zip at: {output_zip_path}")
    start_zip = time.time()
    try:
        with zipfile.ZipFile(
            output_zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9
        ) as zip_out:
            print("\nAdding files to output zip:")
            for file_path in fastled_js_dir.rglob("*"):
                if file_path.is_file():
                    arc_path = file_path.relative_to(fastled_js_dir)
                    print(f"  Adding: {arc_path}")
                    zip_out.write(file_path, arc_path)
    except zipfile.BadZipFile as e:
        print(f"Error creating zip file: {e}")
        return HTTPException(status_code=500, detail=f"Failed to create zip file: {e}")
    except zlib.error as e:
        print(f"Compression error: {e}")
        return HTTPException(
            status_code=500, detail=f"Zip compression failed - zlib error: {e}"
        )
    except Exception as e:
        print(f"Unexpected error creating zip: {e}")
        return HTTPException(status_code=500, detail=f"Failed to create zip file: {e}")
    zip_time = time.time() - start_zip
    print(f"Zip file created in {zip_time:.2f}s")

    def cleanup_files():
        if output_zip_path.exists():
            output_zip_path.unlink()
        if temp_zip_dir:
            shutil.rmtree(temp_zip_dir, ignore_errors=True)
        if temp_src_dir:
            shutil.rmtree(temp_src_dir, ignore_errors=True)

    background_tasks.add_task(cleanup_files)

    return FileResponse(
        path=output_zip_path,
        media_type="application/zip",
        filename="fastled_output.zip",
        background=background_tasks,
    )


# on startup
@app.on_event("startup")
def startup_event():
    sync_source_directory_if_volume_is_mapped()
    if _LIVE_GIT_UPDATES_ENABLED:
        Timer(
            _LIVE_GIT_UPDATES_INTERVAL, sync_live_git_to_target
        ).start()  # Start the periodic git update
    else:
        print("Auto updates disabled")


@app.get("/", include_in_schema=False)
async def read_root() -> RedirectResponse:
    """Redirect to the /docs endpoint."""
    return RedirectResponse(url="/docs")


@app.get("/healthz")
async def healthz() -> dict:
    """Health check endpoint."""
    return {"status": "ok"}


if _ALLOW_SHUTDOWN:

    @app.get("/shutdown")
    async def shutdown() -> dict:
        """Shutdown the server."""
        print("Shutting down server...")
        disk_cache.close()
        os._exit(0)
        return {"status": "ok"}
    
@app.get("/settings")
async def settings() -> dict:
    """Get the current settings."""
    settings = {
        "ALLOW_SHUTDOWN": _ALLOW_SHUTDOWN,
        "NO_SKETCH_CACHE": _NO_SKETCH_CACHE,
        "LIVE_GIT_UPDATES_ENABLED": _LIVE_GIT_UPDATES_ENABLED,
        "LIVE_GIT_UPDATES_INTERVAL": _LIVE_GIT_UPDATES_INTERVAL,
        "UPLOAD_LIMIT": _UPLOAD_LIMIT,
    }
    return settings


# THIS MUST NOT BE ASYNC!!!!
@app.post("/compile/wasm")
def compile_wasm(
    file: UploadFile = File(...),
    authorization: str = Header(None),
    build: str = Header(None),
    profile: str = Header(None),
    background_tasks: BackgroundTasks = BackgroundTasks(),
) -> FileResponse:
    """Upload a file into a temporary directory."""
    if build is not None:
        build = build.lower()

    if build not in ["quick", "release", "debug", None]:
        raise HTTPException(
            status_code=400,
            detail="Invalid build mode. Must be one of 'quick', 'release', or 'debug' or omitted",
        )
    do_profile: bool = False
    if profile is not None:
        do_profile = profile.lower() == "true" or profile.lower() == "1"
    print(f"Build mode is {build}")
    build = build or "quick"
    print(f"Starting upload process for file: {file.filename}")

    if not _TEST and authorization != _AUTH_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")

    if file is None:
        raise HTTPException(status_code=400, detail="No file uploaded.")

    if file.filename is None:
        raise HTTPException(status_code=400, detail="No filename provided.")

    if not file.filename.endswith(".zip"):
        raise HTTPException(
            status_code=400, detail="Uploaded file must be a zip archive."
        )

    temp_zip_dir = None
    temp_src_dir = None

    try:
        # Create temporary directories - one for zip, one for source
        temp_zip_dir = tempfile.mkdtemp()
        temp_src_dir = tempfile.mkdtemp()
        print(
            f"Created temporary directories:\nzip_dir: {temp_zip_dir}\nsrc_dir: {temp_src_dir}"
        )

        file_path = Path(temp_zip_dir) / file.filename
        print(f"Saving uploaded file to: {file_path}")

        # Simple file save since size is already checked by middleware
        with open(file_path, "wb") as f:
            shutil.copyfileobj(file.file, f)

        print("extracting zip file...")
        hash_value: str | None = None
        with zipfile.ZipFile(file_path, "r") as zip_ref:
            zip_ref.extractall(temp_src_dir)
            try:
                hash_value = generate_hash_of_project_files(Path(temp_src_dir))
            except Exception as e:
                warnings.warn(
                    f"Error generating hash: {e}, fast cache access is disabled for this build."
                )

        def on_files_changed() -> None:
            print("Source files changed, clearing cache")
            disk_cache.clear()

        sync_source_directory_if_volume_is_mapped(callback=on_files_changed)

        entry: bytes | None = None
        if hash_value is not None:
            print(f"Hash of source files: {hash_value}")
            entry = try_get_cached_zip(hash_value)
        if entry is not None:
            print("Returning cached zip file")
            # Create a temporary file for the cached data
            tmp_file = NamedTemporaryFile(delete=False)
            tmp_file.write(entry)
            tmp_file.close()

            def cleanup_temp():
                try:
                    os.unlink(tmp_file.name)
                except:  # noqa: E722
                    pass

            background_tasks.add_task(cleanup_temp)

            return FileResponse(
                path=tmp_file.name,
                media_type="application/zip",
                filename="fastled_output.zip",
                background=background_tasks,
            )

        print("\nContents of source directory:")
        for path in Path(temp_src_dir).rglob("*"):
            print(f"  {path}")
        out = compile_source(
            Path(temp_src_dir),
            file_path,
            background_tasks,
            build,
            do_profile,
            hash_value,
        )
        if isinstance(out, HTTPException):
            print("Raising HTTPException")
            raise out
        # Cache the compiled zip file
        out_path = Path(out.path)
        data = out_path.read_bytes()
        if hash_value is not None:
            cache_put(hash_value, data)
        return out
    except HTTPException as e:
        import traceback
        stacktrace = traceback.format_exc()
        print(f"HTTPException in upload process: {str(e)}\n{stacktrace}")
        raise e

    except Exception as e:
        print(f"Error in upload process: {str(e)}")
        # Clean up in case of error
        if temp_zip_dir:
            shutil.rmtree(temp_zip_dir, ignore_errors=True)
        if temp_src_dir:
            shutil.rmtree(temp_src_dir, ignore_errors=True)
        raise HTTPException(
            status_code=500,
            detail=f"Upload process failed: {str(e)}\nTrace: {e.__traceback__}",
        )
