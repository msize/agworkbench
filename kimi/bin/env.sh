# agworkbench (#65): sourced through BASH_ENV by every non-interactive bash Kimi Code starts. Git
# Bash's launcher (Git\bin\bash.exe) puts /mingw64/bin and /usr/bin first on PATH, ahead of anything
# inherited, so the shim directory has to be put back in front here.
case ":$PATH:" in
    *":@@SHIM_DIR@@:"*) ;;
    *) PATH="@@SHIM_DIR@@:$PATH" ;;
esac
export PATH
