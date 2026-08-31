@{
    Version = "b10470"
    Assets = @(
        @{
            Name = "cpu"
            File = "llama-b10470-bin-win-cpu-x64.zip"
            Sha256 = "A31F1F317813AE7E044BE183E0A20B90E78A80C0E97EE11A8B32A014ECCD5043"
            Size = 18470203
            RequiredDll = ""
        }
        @{
            Name = "vulkan"
            File = "llama-b10470-bin-win-vulkan-x64.zip"
            Sha256 = "2E89637B30E0E2F90D4ED486118E8642F60625B1DBEBB9BA3A30BC4100306FC9"
            Size = 34815594
            RequiredDll = "ggml-vulkan.dll"
        }
        # The source launcher needs an OpenVINO-capable llama-server even when
        # the user does not build the full installer. This is a local-only
        # bootstrap asset; release builds replace it with the validated
        # no-NPUW runtime produced by build_npu_runtime.ps1 below.
        @{
            Name = "openvino"
            File = "llama-b10470-bin-win-openvino-2026.2.1-x64.zip"
            Sha256 = "671B0A0C8D5F58E20DA178732435617B182D7127E62080D2CBE270A7A0D69EBDE"
            Size = 80730898
            RequiredDll = "ggml-openvino.dll"
            RequiredDlls = @(
                "ggml-openvino.dll"
                "openvino.dll"
                "openvino_intel_npu_plugin.dll"
                "openvino_intel_npu_compiler_loader.dll"
            )
            SourceOnly = $true
        }
    )
}
