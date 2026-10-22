apiVersion: batch/v1
kind: Job
metadata:
  name: mlops-backup-${BACKUP_JOB_SUFFIX}
  namespace: mlops
  labels:
    app: mlops-backup
spec:
  backoffLimit: 0
  activeDeadlineSeconds: 1800
  ttlSecondsAfterFinished: 86400
  template:
    metadata:
      labels:
        app: mlops-backup
    spec:
      restartPolicy: Never
      serviceAccountName: mlflow-backup
      securityContext:
        fsGroup: 70
      nodeSelector:
        eks.amazonaws.com/nodegroup: system
      containers:
        - name: backup
          image: ${BACKUP_IMAGE}
          imagePullPolicy: IfNotPresent
          args: ["${BACKUP_MODE}", "--key", "${BACKUP_KEY}"]
          env:
            - name: PGHOST
              value: postgres.mlops.svc.cluster.local
            - name: PGUSER
              value: mlflow
            - name: PGDATABASE
              value: mlflow
            - name: PGPASSWORD
              valueFrom:
                secretKeyRef:
                  name: mlflow-db
                  key: password
            - name: BACKUP_BUCKET
              value: ${BACKUP_BUCKET}
            - name: AWS_DEFAULT_REGION
              value: ap-southeast-1
          resources:
            requests:
              cpu: 100m
              memory: 256Mi
              ephemeral-storage: 1Gi
            limits:
              cpu: 500m
              memory: 512Mi
              ephemeral-storage: 4Gi
          volumeMounts:
            - name: scratch
              mountPath: /tmp
      volumes:
        - name: scratch
          emptyDir:
            sizeLimit: 4Gi
