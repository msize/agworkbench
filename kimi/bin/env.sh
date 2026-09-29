# agworkbench (#65): sourced through BASH_ENV by every non-interactive bash Kimi Code starts. Git
# Bash's launcher (Git\bin\bash.exe) puts /mingw64/bin and /usr/bin first on PATH, ahead of the
# inherited Windows PATH - where the pane already put the shim directory - so it is moved back to
# the front here: dropped wherever else it appears, then prepended.
shim='@@SHIM_DIR@@'
case "$PATH" in
    "$shim":*) ;;
    *)
        rest=":$PATH:"
        rest="${rest//":$shim:"/:}"      # quoted: a [ or * in the path is literal, not a pattern
        rest="${rest#:}"
        PATH="$shim:${rest%:}"
        ;;
esac
export PATH
