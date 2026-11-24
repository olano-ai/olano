# Interactive-shell defaults, sourced by /etc/profile (login shells) — which is
# what the dashboard's Terminal opens (runtime/terminal.py) and what a
# `docker exec -it` gets. Copied into BOTH images: the exec sandbox (where a
# managed box's terminal lands) and the runtime container (where a self-hosted
# box's terminal lands when no sandbox is running), so the surface behaves the
# same either way.
#
# It exists because the useful half of "a real terminal" is not in the kernel's
# PTY, it is in the shell's line editor: Tab completing a path, ↑ recalling the
# last command. Debian ships bash-completion but leaves the loader commented out
# in /etc/bash.bashrc, and a fresh container has no ~/.bashrc worth trusting, so
# the behaviour is wired here where the image controls it.
#
# Guarded on $BASH_VERSION throughout: /bin/sh (dash) sources this file too and
# understands none of it.

# The shared tool layer (bind-mounted by the runtime at /opt/agent-tools; see
# olano/backends/interpreter_shims.py) on PATH for interactive shells too. The
# agent's `execute` gets it through an export prefix on every command, and a
# shell opened by hand (`docker exec -it … sh -l`) should find the same pip
# --user scripts, npm globals and seeded CLIs. The runtime-written `shims/`
# go LAST: they only fill in a name the image lacks (`python` on a python3-only
# image), never shadow a real one. POSIX — dash sources this too. Absent in the
# runtime container (no such dir), so it is a no-op there.
if [ -d /opt/agent-tools ]; then
    case ":$PATH:" in
        *:/opt/agent-tools/bin:*) ;;
        *) PATH="/opt/agent-tools/python/bin:/opt/agent-tools/npm/bin:/opt/agent-tools/bin:$PATH" ;;
    esac
    case ":$PATH:" in
        *:/opt/agent-tools/shims:*) ;;
        *) PATH="$PATH:/opt/agent-tools/shims" ;;
    esac
    export PATH
fi

if [ -n "${BASH_VERSION-}" ]; then
    case "$-" in
        *i*)
            # Programmable completion (git subcommands, apt packages, ssh hosts…).
            # Filename completion is readline's own and needs none of this; this
            # is the layer on top. `shopt -oq posix` guard is upstream's.
            if ! shopt -oq posix; then
                if [ -r /usr/share/bash-completion/bash_completion ]; then
                    . /usr/share/bash-completion/bash_completion
                elif [ -r /etc/bash_completion ]; then
                    . /etc/bash_completion
                fi
            fi

            # History worth arrowing through: keep a deep one, append rather than
            # clobber (several terminal tabs share this container), and drop
            # duplicate/space-prefixed entries.
            HISTSIZE=10000
            HISTFILESIZE=20000
            HISTCONTROL=ignoreboth
            shopt -s histappend
            shopt -s checkwinsize

            # A prompt that says where you are — the working directory is the one
            # thing a browser terminal cannot show anywhere else. \h, not a
            # literal name: this file ships in two different containers.
            PS1='\[\033[38;5;209m\]\u@\h\[\033[0m\]:\[\033[38;5;39m\]\w\[\033[0m\]\$ '
            ;;
    esac
fi
