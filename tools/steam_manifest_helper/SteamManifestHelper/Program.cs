using SteamManifestHelper;

return await Cli.RunAsync(args, Console.Out, Console.Error, () => new SteamKitGateway(Console.Error), CancellationToken.None);
