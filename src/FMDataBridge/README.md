# FM26 Data Bridge

Read-only BepInEx/IL2CPP probe for Football Manager 26.

The probe constructs a typed `TeamReference` directly from the configured FM
team UID and opens read-only native data channels for fixture properties. It
does not scan database indexes and does not call `InteropReference.SetValue`,
`Bindings.Set`, or any game-data mutation API.

Build:

```powershell
dotnet build .\src\FMDataBridge\FMDataBridge.csproj -c Release
```

The compiled DLL is loaded by BepInEx on the next FM launch. Probe output is
written as JSONL under `U:\Work\FM\data\bridge` by default.
