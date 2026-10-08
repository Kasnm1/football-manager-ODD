using System.Text.Json;
using BepInEx;
using BepInEx.Unity.IL2CPP;
using FM.Game;
using FM.GamePlugin;
using FM.UI;
using Il2CppInterop.Runtime.Injection;
using SI.Bindable;
using SI.Bindable.Reference.Core;
using SI.Core;
using SI.Interop;
using UnityEngine;

namespace FMDataBridge;

[BepInPlugin(PluginId, PluginName, PluginVersion)]
public sealed class Plugin : BasePlugin
{
    public const string PluginId = "local.fm26.data-bridge";
    public const string PluginName = "FM26 Data Bridge";
    public const string PluginVersion = "0.2.0";

    private const uint UniqueIdProperty = 1970170212;
    private const ulong ChannelKeyBase = 0x464D260000000000;

    private static readonly (string Name, uint PropertyId)[] TeamRequests =
    {
        ("UniqueId", UniqueIdProperty),
        ("FixtureList", 1413904460),
        ("FixtureListElements", 1935767927),
        ("Next5Fixtures", 1765160280),
        ("NextFixture", 1852130921),
        ("NextFixtureListElement", 1229017703)
    };

    private static Plugin _instance;
    private readonly Dictionary<ulong, string> _channelNames = new();
    private readonly Dictionary<ulong, Bindings.Key> _channelKeys = new();
    private readonly HashSet<ulong> _received = new();
    private GameInteropSubsystem _interop;
    private ValueChangedWithSizeCallback _callback;
    private TeamReference _teamReference;
    private string _outputPath = string.Empty;
    private int _targetUid;
    private float _startAfter;
    private float _timeoutAt;
    private bool _started;
    private bool _finished;
    private ProbeBehaviour _behaviour;

    public override void Load()
    {
        _instance = this;
        var outputDirectory = Config.Bind(
            "Output",
            "Directory",
            @"U:\Work\FM\data\bridge",
            "Directory for read-only channel output.").Value;
        _targetUid = Config.Bind("Probe", "TeamUid", 1190, "FM team UID to query.").Value;

        Directory.CreateDirectory(outputDirectory);
        _outputPath = Path.Combine(outputDirectory, $"fm26_channel_probe_{System.DateTime.Now:yyyyMMdd_HHmmss}.jsonl");
        _startAfter = Time.realtimeSinceStartup + 5f;

        ClassInjector.RegisterTypeInIl2Cpp<ProbeBehaviour>();
        var gameObject = new GameObject("FM26DataBridge");
        UnityEngine.Object.DontDestroyOnLoad(gameObject);
        _behaviour = gameObject.AddComponent<ProbeBehaviour>();

        WriteEvent("probe_loaded", new
        {
            target_uid = _targetUid,
            process_id = Environment.ProcessId,
            read_only = true,
            mode = "direct_uid_channels"
        });
        Log.LogInfo($"{PluginName} {PluginVersion} loaded in direct read-only mode");
    }

    internal static void TickCurrent()
    {
        _instance?.Tick();
    }

    private void Tick()
    {
        if (_finished || Time.realtimeSinceStartup < _startAfter)
        {
            return;
        }

        if (!_started)
        {
            var interop = PluginContextActionsModule.m_interopSubsystem;
            if (interop == null || interop.Pointer == IntPtr.Zero)
            {
                return;
            }

            StartRequests(interop);
            return;
        }

        if (Time.realtimeSinceStartup >= _timeoutAt)
        {
            Finish("timeout");
        }
    }

    private void StartRequests(GameInteropSubsystem interop)
    {
        _started = true;
        _interop = interop;
        _timeoutAt = Time.realtimeSinceStartup + 20f;

        var ids = new Il2CppSystem.Collections.Generic.List<InteropReference.Pair>();
        ids.Add(new InteropReference.Pair(UniqueIdProperty, _targetUid));
        _teamReference = new TeamReference(ids);

        System.Action<
            Il2CppSystem.ReadOnlySpan<ulong>,
            Il2CppSystem.Collections.Generic.List<TypedValue>,
            Il2CppSystem.ReadOnlySpan<long>> managedCallback = OnChannelDataChanged;
        _callback = managedCallback;
        _interop.add_OnChannelDataChange(_callback);

        for (var i = 0; i < TeamRequests.Length; i++)
        {
            var request = TeamRequests[i];
            var rawKey = ChannelKeyBase + (ulong)i + 1;
            var key = new Bindings.Key(rawKey);
            _channelNames[rawKey] = request.Name;
            _channelKeys[rawKey] = key;
            _interop.OpenChannel(_teamReference, new PropertyID(request.PropertyId), key);
        }

        WriteEvent("channels_opened", new
        {
            target_uid = _targetUid,
            requests = TeamRequests.Select(request => new { name = request.Name, property_id = request.PropertyId })
        });
    }

    private void OnChannelDataChanged(
        Il2CppSystem.ReadOnlySpan<ulong> keys,
        Il2CppSystem.Collections.Generic.List<TypedValue> values,
        Il2CppSystem.ReadOnlySpan<long> sizes)
    {
        var count = Math.Min(keys.Length, values?.Count ?? 0);
        for (var i = 0; i < count; i++)
        {
            var rawKey = keys[i];
            if (!_channelNames.TryGetValue(rawKey, out var name))
            {
                continue;
            }

            var value = values[i];
            var size = i < sizes.Length ? sizes[i] : -1;
            _received.Add(rawKey);
            WriteEvent("channel_value", DescribeValue(name, rawKey, size, value));
        }

        if (_received.Count >= TeamRequests.Length)
        {
            Finish("all_values_received");
        }
    }

    private object DescribeValue(string name, ulong rawKey, long size, TypedValue value)
    {
        if (value == null || value.Pointer == IntPtr.Zero)
        {
            return new { name, key = rawKey, size, is_null = true };
        }

        string dataType;
        string asString;
        string objectType;
        string objectString;

        try { dataType = value.DataType?.FullName ?? string.Empty; }
        catch (Exception ex) { dataType = $"<error:{ex.GetType().Name}>"; }
        try { asString = value.AsString() ?? string.Empty; }
        catch (Exception ex) { asString = $"<error:{ex.GetType().Name}>"; }

        try
        {
            var raw = value.Get();
            objectType = raw?.GetIl2CppType()?.FullName ?? string.Empty;
            objectString = raw?.ToString() ?? string.Empty;
        }
        catch (Exception ex)
        {
            objectType = string.Empty;
            objectString = $"<error:{ex.GetType().Name}:{ex.Message}>";
        }

        return new
        {
            name,
            key = rawKey,
            size,
            is_null = value.IsNull,
            data_type = dataType,
            as_string = asString,
            object_type = objectType,
            object_string = objectString
        };
    }

    private void Finish(string reason)
    {
        if (_finished)
        {
            return;
        }

        _finished = true;
        if (_interop != null && _interop.Pointer != IntPtr.Zero)
        {
            foreach (var key in _channelKeys.Values)
            {
                try { _interop.CloseChannel(key); }
                catch { }
            }

            if (_callback != null)
            {
                try { _interop.remove_OnChannelDataChange(_callback); }
                catch { }
            }
        }

        WriteEvent("probe_finished", new
        {
            reason,
            received = _received.Count,
            requested = TeamRequests.Length
        });
        Log.LogInfo($"Direct channel probe finished ({reason}); output: {_outputPath}");
        if (_behaviour != null)
        {
            _behaviour.enabled = false;
        }
    }

    private void WriteEvent(string eventName, object payload)
    {
        try
        {
            File.AppendAllText(
                _outputPath,
                JsonSerializer.Serialize(new { timestamp = DateTimeOffset.Now, @event = eventName, payload })
                + Environment.NewLine);
        }
        catch (Exception ex)
        {
            Log.LogError($"Unable to write channel output: {ex}");
        }
    }
}

public sealed class ProbeBehaviour : MonoBehaviour
{
    public ProbeBehaviour(IntPtr pointer) : base(pointer)
    {
    }

    public void Update()
    {
        Plugin.TickCurrent();
    }
}
