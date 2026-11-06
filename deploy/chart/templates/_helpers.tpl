{{/* Production website requests must traverse the same weighted ALB as analysis traffic. */}}
{{- define "market-intel.marketTrafficUrl" -}}
{{- if .Values.rollouts.routeDashboardThroughAlb -}}
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
      {{- range $key, $value := $service.env }}
      - name: {{ $key }}
        {{- if and (eq $.name "dashboard") (eq $key "MARKET_API_URL") }}
        value: {{ include "market-intel.marketTrafficUrl" $.root | quote }}
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
    startupProbe:
      httpGet:
        path: {{ $service.healthPath }}
        port: http
      failureThreshold: 30
      periodSeconds: 5
    readinessProbe:
      httpGet:
        path: {{ $service.healthPath }}
        port: http
      periodSeconds: 10
    livenessProbe:
      httpGet:
        path: {{ $service.healthPath }}
        port: http
      periodSeconds: 20
volumes:
  - name: tmp
    emptyDir:
      sizeLimit: 256Mi
{{- end -}}
