# Third-party components

This application uses the unmodified Inochi2D SDK 0.8.6 source at commit
`dda3a8f005f808104caa43183ab8753fb989e30f`, compiled with a project-owned build
recipe and C interface. Its original BSD-2-Clause notice is included.

The D library incorporates imagefmt 2.1.2, inmath 1.3.2, fghj 1.0.2,
numem 0.11.3, mir-core 1.7.4, mir-algorithm 3.22.4,
i2d-opengl 1.0.0 and bindbc-loader 1.0.3. Their supplied license texts are
included. i2d-opengl and bindbc-loader use the Boost Software License 1.0.

BindBC attribution: Copyright 2018–2021 Michael D. Parker;
bindbc-loader also Copyright 2023 Aya Partridge;
i2d-opengl includes contributions by the Inochi2D Project.

SDL2.dll is SDL 2.28.2, Copyright (C) 1997–2023 Sam Lantinga and contributors,
under the zlib license. It is packaged alongside this application and is not
loaded from an installed Creator application at runtime. License source:
https://github.com/libsdl-org/SDL/blob/release-2.28.2/LICENSE.txt

The D runtime and Phobos were statically linked using LDC 1.43.0. The LDC
distribution's component license overview and the Boost license are included.
Build tools are not part of this runtime package.

Local companion components: llama.cpp b11669 and whisper.cpp v1.8.3 (MIT),
official Qwen3 GGUF weights (Apache-2.0), CPython 3.13.13 embedded
(Python and component licenses), Microsoft WebView2 SDK static loader
1.0.4258.31 (BSD-3-Clause), and LLVM OpenMP (included upstream notice).
whisper ggml-small-q5_1.bin uses the upstream whisper.cpp MIT terms.
NVIDIA CUDA 12.4 runtime/cuBLAS redistributables accompany the CUDA EULA.
Files are original upstream binaries; no CUDA toolkit installation is required.
WebView2 Evergreen and Windows SAPI voices are system components governed by
their respective Microsoft terms. Windows voice assets are not copied into this package.
Microsoft Fixed WebView2 Runtime 156.0.4314.42 x64 accompanies this application
under its included original EULA. Read the bundled SmartScreen product notice
and original Section 8 in licenses/. Fixed Runtime security fixes require a
later application package update.

Bundled CPU speech uses sherpa-onnx 2.0.0 (Apache-2.0), ONNX Runtime (MIT,
with original third-party notices), MeloTTS Chinese-English female INT8
weights (MIT), and accompanying frontend/dictionary components. Original
notices and license coverage accompany backend/tts/licenses/.
LocalTtsHelper.exe is a project-owned Unicode adapter using the unchanged
upstream C API. This package does not include Piper or eSpeak voice data.
Microsoft Visual C++ v14 runtime 14.50.35710 app-local redistributable DLLs
are included alongside the native inference workers; original Microsoft license
terms are included. AsrHelper.exe is project-owned Windows Unicode I/O around
the unmodified whisper.cpp v1.8.3 C API, with the model loaded from a buffer.

Model sources, pinned revisions, download sizes and SHA256 hashes are in
companion/model-manifest.json. No external account or inference API is used.

Character artwork and reference material retain their original rights.
These software licenses do not grant rights to the character artwork.
