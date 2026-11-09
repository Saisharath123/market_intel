{{- define "market-intel.apiDns" -}}
{{- printf "%s.%s.svc.cluster.local" .name .root.Release.Namespace -}}
{{- end -}}

{{- define "market-intel.internalApiUrl" -}}
{{- $api := index .root.Values.services .name -}}
{{- printf "https://%s:%v" (include "market-intel.apiDns" .) $api.port -}}
{{- end -}}

{{/* Production website requests must traverse the same weighted ALB as analysis traffic. */}}
{{- define "market-intel.marketTrafficUrl" -}}
{{- if (default (dict) .Values.internalTls).enabled -}}
{{- if or (ne .Values.environment "staging") (ne .Release.Namespace "market-intel-staging") .Values.rollouts.enabled .Values.rollouts.routeDashboardThroughAlb -}}
{{- fail "internalTls.enabled is supported only for staging with direct API traffic" -}}
{{- end -}}
{{- include "market-intel.internalApiUrl" (dict "name" "market-api" "root" .) -}}
{{- else if .Values.rollouts.routeDashboardThroughAlb -}}
{{- if not .Values.rollouts.enabled -}}
{{- fail "rollouts.routeDashboardThroughAlb requires rollouts.enabled: true" -}}
{{- end -}}
{{- $hostname := required "rollouts.apiHostname is required for weighted dashboard traffic" .Values.rollouts.apiHostname -}}
{{- if not (regexMatch "^[a-zA-Z0-9]([a-zA-Z0-9.-]*[a-zA-Z0-9])?$" $hostname) -}}
{{- fail "rollouts.apiHostname must be a DNS hostname without a scheme, path or port" -}}
{{- end -}}
{{- printf "http://%s" $hostname -}}
{{- else -}}
{{- (index .Values.services "dashboard").env.MARKET_API_URL -}}
{{- end -}}
{{- end -}}

{{/* Deployments and Rollouts share the same pod security and resource settings. */}}
{{- define "market-intel.podSpec" -}}
{{- $service := .service -}}
{{- $image := required (printf "services.%s.image is required" .name) $service.image -}}
{{- $tls := (default (dict) .root.Values.internalTls).enabled -}}
{{- $dashboard := eq .name "dashboard" -}}
{{- $apiTls := and $tls (not $dashboard) -}}
{{- if and .root.Values.requireImageDigest (not (regexMatch "^.+@sha256:[a-f0-9]{64}$" $image)) -}}
{{- fail (printf "services.%s.image must use an immutable sha256 digest" .name) -}}
{{- end -}}
automountServiceAccountToken: false
terminationGracePeriodSeconds: 60
securityContext:
  runAsNonRoot: true
  runAsUser: 10001
  runAsGroup: 10001
  fsGroup: 10001
  seccompProfile:
    type: RuntimeDefault
topologySpreadConstraints:
  - maxSkew: 1
    topologyKey: kubernetes.io/hostname
    whenUnsatisfiable: DoNotSchedule
    labelSelector:
      matchLabels:
        app: {{ .name }}
containers:
  - name: {{ .name }}
    image: {{ $image | quote }}
    imagePullPolicy: IfNotPresent
    ports:
      - name: http
        containerPort: {{ $service.port }}
    env:
      - name: PYTHONDONTWRITEBYTECODE
        value: '1'
      - name: HOME
        value: /tmp
      - name: XDG_CACHE_HOME
        value: /tmp/cache
      {{- if $apiTls }}
      - name: UVICORN_SSL_CERTFILE
        value: /var/run/market-intel/tls/tls.crt
      - name: UVICORN_SSL_KEYFILE
        value: /var/run/market-intel/tls/tls.key
      {{- else if $tls }}
      - name: REQUESTS_CA_BUNDLE
        value: /var/run/market-intel/ca/ca.crt
      {{- end }}
      {{- range $key, $value := $service.env }}
      - name: {{ $key }}
        {{- if and (eq $.name "dashboard") (eq $key "MARKET_API_URL") }}
        value: {{ include "market-intel.marketTrafficUrl" $.root | quote }}
        {{- else if and $tls $dashboard (eq $key "NEWS_API_URL") }}
        value: {{ include "market-intel.internalApiUrl" (dict "name" "news-api" "root" $.root) | quote }}
        {{- else }}
        value: {{ $value | quote }}
        {{- end }}
      {{- end }}
    resources:
      requests:
        cpu: {{ default "100m" $service.cpuRequest }}
        memory: {{ default "128Mi" $service.memoryRequest }}
      limits:
        cpu: '1'
        memory: {{ $service.memory }}
    securityContext:
      allowPrivilegeEscalation: false
      readOnlyRootFilesystem: true
      capabilities:
        drop: [ALL]
    volumeMounts:
      - name: tmp
        mountPath: /tmp
      {{- if $apiTls }}
      - name: internal-tls
        mountPath: /var/run/market-intel/tls
        readOnly: true
      {{- else if $tls }}
      - name: internal-ca
        mountPath: /var/run/market-intel/ca
        readOnly: true
      {{- end }}
    startupProbe:
      httpGet:
        path: {{ $service.healthPath }}
        port: http
        {{- if $apiTls }}
        scheme: HTTPS
        {{- end }}
      failureThreshold: 30
      periodSeconds: 5
    readinessProbe:
      httpGet:
        path: {{ $service.healthPath }}
        port: http
        {{- if $apiTls }}
        scheme: HTTPS
        {{- end }}
      periodSeconds: 10
    livenessProbe:
      httpGet:
        path: {{ $service.healthPath }}
        port: http
        {{- if $apiTls }}
        scheme: HTTPS
        {{- end }}
      periodSeconds: 20
volumes:
  - name: tmp
    emptyDir:
      sizeLimit: 256Mi
  {{- if $apiTls }}
  - name: internal-tls
    secret:
      secretName: {{ printf "%s-tls" .name | quote }}
      defaultMode: 288
  {{- else if $tls }}
  - name: internal-ca
    configMap:
      name: market-intel-ca
      items:
        - key: ca.crt
          path: ca.crt
  {{- end }}
{{- end -}}
