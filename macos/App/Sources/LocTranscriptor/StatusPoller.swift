// Reads /api/status (counters, models_ready, ui_lang) from the service with the access
// key. Ephemeral session: no cache, no cookie storage, no proxy, no redirects.

import AppCore
import Foundation

/// Forbids redirects: requests must never leave 127.0.0.1.
final class NoRedirects: NSObject, URLSessionTaskDelegate, Sendable {
    func urlSession(
        _ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
        newRequest request: URLRequest
    ) async -> URLRequest? { nil }
}

@MainActor
final class StatusPoller {
    private let session: URLSession = {
        let config = URLSessionConfiguration.ephemeral
        config.requestCachePolicy = .reloadIgnoringLocalCacheData
        config.urlCache = nil
        config.httpCookieStorage = nil
        config.httpShouldSetCookies = false
        config.connectionProxyDictionary = [:]
        config.timeoutIntervalForRequest = 2
        config.timeoutIntervalForResource = 2
        return URLSession(configuration: config, delegate: NoRedirects(), delegateQueue: nil)
    }()

    func fetch(origin: LocalOrigin, key: String) async -> ServiceStatus? {
        var request = URLRequest(url: origin.url(path: "/api/status"))
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.httpShouldHandleCookies = false
        request.setValue("tb_key=\(key)", forHTTPHeaderField: "Cookie")
        guard let (data, response) = try? await session.data(for: request),
              (response as? HTTPURLResponse)?.statusCode == 200 else { return nil }
        return ServiceStatus.decode(data)
    }
}
