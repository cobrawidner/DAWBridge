"""PyInstaller entry point - a plain top-level script so it doesn't hit
package-relative-import issues when frozen. Just delegates into the real
package.
"""
from dawbridge.gui import main

if __name__ == "__main__":
    raise SystemExit(main())
