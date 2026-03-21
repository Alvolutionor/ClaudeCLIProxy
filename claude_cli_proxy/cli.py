"""Claude CLI subprocess wrapper."""

import asyncio
import os
import tempfile

from .config import ProxyConfig


class ClaudeCLI:
    """Manages Claude CLI subprocess calls with concurrency control."""

    def __init__(self, config: ProxyConfig):
        self.config = config
        self._semaphore = asyncio.Semaphore(config.max_concurrent)

    def _build_env(self) -> dict[str, str]:
        """Build environment variables for the CLI subprocess."""
        env = os.environ.copy()
        env.pop("CLAUDECODE", None)
        env["CLAUDE_CODE_GIT_BASH_PATH"] = self.config.git_bash_path
        return env

    async def call(self, prompt: str, model: str = "") -> str:
        """Call Claude CLI in pipe mode and return the response text.

        Args:
            prompt: The prompt text to send.
            model: Model identifier. Falls back to config default if empty.

        Returns:
            The CLI stdout as a string.

        Raises:
            RuntimeError: If the CLI exits with a non-zero code.
            asyncio.TimeoutError: If the call exceeds the configured timeout.
        """
        model = model or self.config.default_model

        # Write prompt to a temp file to avoid shell escaping issues
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8"
        ) as f:
            f.write(prompt)
            prompt_file = f.name

        try:
            env = self._build_env()
            model_args = ["--model", model] if model else []

            async with self._semaphore:
                proc = await asyncio.create_subprocess_exec(
                    "claude", "-p", *model_args,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                )
                with open(prompt_file, "rb") as pf:
                    prompt_bytes = pf.read()

                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(input=prompt_bytes),
                    timeout=self.config.cli_timeout,
                )

            if proc.returncode != 0:
                err = stderr.decode("utf-8", errors="replace").strip()
                raise RuntimeError(
                    f"claude CLI error (code {proc.returncode}): {err}"
                )

            return stdout.decode("utf-8", errors="replace").strip()
        finally:
            os.unlink(prompt_file)
