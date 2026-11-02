using System.Net;
using SteamKit2;

namespace SteamManifestHelper;

/// <summary>DepotDownloader's CDN rule (ContentDownloader.cs:815-824): a 403 with no
/// CDN auth token yet means "get one and try again", once.</summary>
public static class CdnAuth
{
    public static async Task<T> WithAuthRetryAsync<T>(Func<string?, Task<T>> download, string? knownToken, Func<Task<string?>> fetchToken)
    {
        try
        {
            return await download(knownToken);
        }
        catch (Exception e) when (knownToken is null && IsForbidden(e))
        {
            var token = await fetchToken();
            if (token is null)
            {
                throw;
            }
            return await download(token);
        }
    }

    public static bool IsForbidden(Exception e) => e switch
    {
        SteamKitWebRequestException web => web.StatusCode == HttpStatusCode.Forbidden,
        HttpRequestException http => http.StatusCode == HttpStatusCode.Forbidden,
        _ => false,
    };
}
