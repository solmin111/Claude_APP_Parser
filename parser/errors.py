class ClError(Exception):

    code = "CX_ERROR"

    def __init__(self, message, **context):
        super().__init__(message)
        self.message = message
        self.context = context

    def to_dict(self):
        return {"code": self.code, "message": self.message, "context": self.context}


class ImageOpenError(ClError):
    code = "IMAGE_OPEN_FAILED"


class MissingSegmentError(ClError):
    code = "SEGMENT_MISSING"


class UnsupportedFormatError(ClError):
    code = "UNSUPPORTED_FORMAT"


class VolumeLockedError(ClError):
    code = "VOLUME_LOCKED"


class LiveAccessError(ClError):
    code = "LIVE_ACCESS_FAILED"


class FilesystemError(ClError):
    code = "FILESYSTEM_ERROR"


class ArtifactNotFoundError(ClError):
    code = "ARTIFACT_NOT_FOUND"


class CorruptArtifactError(ClError):
    code = "ARTIFACT_CORRUPT"
