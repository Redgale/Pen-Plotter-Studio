#!/bin/bash
# Builds PenPlotterStudio-x86_64.AppImage from source.
# Run from anywhere -- it locates the repo root relative to this script.
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
BUILD_DIR="${SCRIPT_DIR}/build"
APPDIR="${SCRIPT_DIR}/PenPlotterStudio.AppDir"
OUT="${SCRIPT_DIR}/PenPlotterStudio-x86_64.AppImage"

echo "==> Repo root: ${REPO_ROOT}"

echo "==> Installing/checking Python dependencies..."
pip install --break-system-packages -q \
    PySide6 opencv-python-headless numpy pyserial pyinstaller

echo "==> Running PyInstaller..."
rm -rf "${BUILD_DIR}" "${APPDIR}" "${SCRIPT_DIR}/dist" "${SCRIPT_DIR}"/*.spec
pyinstaller --noconfirm --onefile --windowed \
    --distpath "${SCRIPT_DIR}/dist" \
    --workpath "${BUILD_DIR}" \
    --specpath "${SCRIPT_DIR}" \
    --name PenPlotterStudio \
    --icon "${REPO_ROOT}/resources/icon_256.png" \
    --add-data "${REPO_ROOT}/resources:resources" \
    "${REPO_ROOT}/src/main.py"

echo "==> Assembling AppDir..."
mkdir -p "${APPDIR}/usr/bin" "${APPDIR}/usr/lib"
cp "${SCRIPT_DIR}/dist/PenPlotterStudio" "${APPDIR}/usr/bin/"
cp "${REPO_ROOT}/resources/icon_256.png" "${APPDIR}/penplotterstudio.png"

cat > "${APPDIR}/penplotterstudio.desktop" << 'EOF'
[Desktop Entry]
Type=Application
Name=PenPlotter Studio
Comment=Convert images into pen-plotter G-code with outline tracing and shading
Exec=PenPlotterStudio
Icon=penplotterstudio
Categories=Graphics;2DGraphics;Engineering;
Terminal=false
StartupWMClass=PenPlotterStudio
EOF

cat > "${APPDIR}/AppRun" << 'EOF'
#!/bin/bash
HERE="$(dirname "$(readlink -f "${0}")")"
export LC_ALL=C.UTF-8
export LANG=C.UTF-8
export LD_LIBRARY_PATH="${HERE}/usr/lib:${LD_LIBRARY_PATH}"
exec "${HERE}/usr/bin/PenPlotterStudio" "$@"
EOF
chmod +x "${APPDIR}/AppRun"

echo "==> Bundling libxcb-cursor and friends (Qt 6.5+ needs these; not every"
echo "    distro has them installed by default, so we ship our own copies)..."
for lib in libxcb-cursor.so.0.0.0 libxcb-render-util.so.0 libxcb-render.so.0 \
           libxcb-image.so.0 libxcb-shm.so.0 libxcb-util.so.1 \
           libXau.so.6 libXdmcp.so.6 libbsd.so.0 libmd.so.0; do
    f=$(find /usr/lib /lib -name "$lib" 2>/dev/null | head -1)
    if [ -n "$f" ]; then
        cp -L "$f" "${APPDIR}/usr/lib/"
    else
        echo "    (warning: $lib not found on this system -- skipping; app may"
        echo "     need it installed on the machine that runs the AppImage)"
    fi
done
if [ -f "${APPDIR}/usr/lib/libxcb-cursor.so.0.0.0" ]; then
    ln -sf libxcb-cursor.so.0.0.0 "${APPDIR}/usr/lib/libxcb-cursor.so.0"
fi

echo "==> Fetching appimagetool if needed..."
APPIMAGETOOL="${SCRIPT_DIR}/appimagetool"
if [ ! -f "${APPIMAGETOOL}" ]; then
    wget -q "https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-x86_64.AppImage" \
        -O "${APPIMAGETOOL}"
    chmod +x "${APPIMAGETOOL}"
fi

echo "==> Building AppImage..."
if "${APPIMAGETOOL}" --version >/dev/null 2>&1; then
    ARCH=x86_64 "${APPIMAGETOOL}" "${APPDIR}" "${OUT}"
else
    # No FUSE available (common in containers/CI) -- extract and run instead.
    (cd "${SCRIPT_DIR}" && "${APPIMAGETOOL}" --appimage-extract >/dev/null 2>&1)
    ARCH=x86_64 "${SCRIPT_DIR}/squashfs-root/AppRun" "${APPDIR}" "${OUT}"
    rm -rf "${SCRIPT_DIR}/squashfs-root"
fi

chmod +x "${OUT}"
echo ""
echo "==> Done: ${OUT}"
