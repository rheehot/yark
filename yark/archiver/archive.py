"""Archive management with metadata/video downloading core"""

from __future__ import annotations
from datetime import datetime
import json
from pathlib import Path
import time
from yt_dlp import YoutubeDL, DownloadError  # type: ignore
from colorama import Style, Fore
import sys
from .reporter import Reporter
from ..errors import ArchiveNotFoundException
from ..logger import _err_msg
from .video.video import Video, Videos
from .comment_author import CommentAuthor
from typing import Optional, Any
from .config import Config, YtDlpSettings
from .converter import Converter
from .migrator import _migrate
from ..utils import ARCHIVE_COMPAT
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, Future
from progress.spinner import PieSpinner

# NOTE: maybe make into dataclass
@dataclass(init=False)
class Archive:
    path: Path
    url: str
    version: int
    videos: Videos
    livestreams: Videos
    shorts: Videos
    reporter: Reporter
    comment_authors: dict[str, CommentAuthor]

    def __init__(
        self,
        path: Path,
        url: str,
        version: int = ARCHIVE_COMPAT,
        videos: Videos | None = None,
        livestreams: Videos | None = None,
        shorts: Videos | None = None,
        comment_authors: dict[str, CommentAuthor] = {},
    ) -> None:
        self.path = path
        self.url = url
        self.version = version
        self.videos = Videos(self) if videos is None else videos
        self.livestreams = Videos(self) if livestreams is None else livestreams
        self.shorts = Videos(self) if shorts is None else shorts
        self.reporter = Reporter(self)
        self.comment_authors = comment_authors

    @staticmethod
    def load(path: Path) -> Archive:
        """Loads existing archive from path"""
        # Check existence
        path = Path(path)
        archive_name = path.name
        print(f"Loading {archive_name} archive..")
        if not path.exists():
            raise ArchiveNotFoundException("Archive doesn't exist")

        # Load config
        encoded = json.load(open(path / "yark.json", "r"))

        # Check version before fully decoding and exit if wrong
        archive_version = encoded["version"]
        if archive_version != ARCHIVE_COMPAT:
            encoded = _migrate(
                archive_version, ARCHIVE_COMPAT, encoded, path, archive_name
            )

        # Decode and return
        return Archive._from_archive_o(encoded, path)

    def metadata(self, config: Config) -> None:
        """Queries YouTube for all channel/playlist metadata to refresh known videos"""
        # Download metadata
        with ThreadPoolExecutor(1) as executor:
            future = executor.submit(self._metadata_download, config)
            _progress_spinner("Downloading metadata..", future)
            res = future.result()

        # Uncomment for saving big dumps for testing
        # with open("demo/dump.json", "w+") as file:
        #     json.dump(res, file)

        # Uncomment for loading big dumps for testing
        # res = json.load(open("demo/dump.json", "r"))

        # Parse metadata
        self._metadata_parse(config, res)

    def _metadata_download(self, config: Config) -> dict[str, Any]:
        """Downloads metadata"""
        # Get settings
        settings = config.settings_md()

        # Pull metadata from youtube
        with YoutubeDL(settings) as ydl:
            for i in range(3):
                try:
                    res: dict[str, Any] = ydl.extract_info(self.url, download=False)
                    return res
                except Exception as exception:
                    # Report error
                    retrying = i != 2
                    _err_dl("metadata", exception, retrying)

                    # Print retrying message
                    if retrying:
                        print(
                            Style.DIM
                            + f"\n  • Retrying metadata download.."
                            + Style.RESET_ALL
                        )

        raise Exception()

    def _metadata_parse(self, config: Config, res: dict[str, Any]) -> None:
        """Parses previously downloaded metadata"""
        # Make buckets to normalize different types of videos
        videos = []
        livestreams = []
        shorts = []

        # Videos only (basic channel or playlist)
        if "entries" not in res["entries"][0]:
            videos = res["entries"]

        # Videos and at least one other (livestream/shorts)
        else:
            for entry in res["entries"]:
                # Find the kind of category this is; youtube formats these as 3 playlists
                kind = entry["title"].split(" - ")[-1].lower()

                # Plain videos
                if kind == "videos":
                    videos = entry["entries"]

                # Livestreams
                elif kind == "live":
                    livestreams = entry["entries"]

                # Shorts
                elif kind == "shorts":
                    shorts = entry["entries"]

                # Unknown 4th kind; youtube might've updated
                else:
                    _err_msg(f"Unknown video kind '{kind}' found", True)

        # Parse metadata
        self._metadata_parse_videos("video", config, videos, self.videos)
        self._metadata_parse_videos("livestream", config, livestreams, self.livestreams)
        self._metadata_parse_videos("shorts", config, shorts, self.shorts)

        # Go through each and report deleted
        self._report_deleted(self.videos)
        self._report_deleted(self.livestreams)
        self._report_deleted(self.shorts)

    def _metadata_parse_videos(
        self,
        kind: str,
        config: Config,
        entries: list[dict[str, Any]],
        videos: Videos,
    ) -> None:
        """Parses metadata for a category of video into it's `videos` bucket"""

        def comp() -> None:
            """Computes the actual parsing, in this function so that it can be ran with the loading spinner"""
            # Parse each video
            for entry in entries:
                self._metadata_parse_video(config, entry, videos)

            # Sort videos by newest
            videos.sort()

        with ThreadPoolExecutor(1) as executor:
            future = executor.submit(comp)
            _progress_spinner(f"Parsing {kind} metadata..", future)

    def _metadata_parse_video(
        self, config: Config, entry: dict[str, Any], videos: Videos
    ) -> None:
        """Parses metadata for one video, creating it or updating it depending on the `videos` already in the bucket"""
        # Skip video if there's no formats available; happens with upcoming videos/livestreams
        if "formats" not in entry or len(entry["formats"]) == 0:
            return

        # Updated intra-loop marker
        updated = False

        # Update video if it exists
        found_video = videos.inner.get(entry["id"])
        if found_video is not None:
            found_video.update(config, entry)
            updated = True
            return

        # Add new video if not
        if not updated:
            video = Video.new(config, self, entry)
            videos.inner[video.id] = video
            self.reporter.added.append(video)

    def download(self, config: Config) -> bool:
        """Downloads all videos which haven't already been downloaded, returning if anything was downloaded"""
        # Prepare; clean out old part files and get settings
        self._clean_parts()
        settings = config.settings_dl(self.path)

        # Retry downloading 5 times in total for all videos
        anything_downloaded = True
        for i in range(5):
            # Try to curate a list and download videos on it
            try:
                # Curate list of non-downloaded videos
                not_downloaded = self._curate(config)

                # Return if there's nothing to download
                if len(not_downloaded) == 0:
                    anything_downloaded = False
                    return False

                # Print curated if this is the first time
                if i == 0:
                    _log_download_count(len(not_downloaded))

                # Launch core to download all curated videos
                self._dl_launch(settings, not_downloaded)

                # Stop if we've got them all
                break

            # Report error and retry/stop
            except Exception as exception:
                # Get around carriage return
                if i == 0:
                    print()

                # Report error
                _err_dl("videos", exception, i != 4)

        # End by converting any downloaded but unsupported video file formats
        if anything_downloaded:
            converter = Converter(self.path / "videos")
            converter.run()

        # Say that something was downloaded
        return True

    def _dl_launch(self, settings: YtDlpSettings, not_downloaded: list[Video]) -> None:
        """Downloads all `not_downloaded` videos passed into it whilst automatically handling privated videos, this is the core of the downloader"""
        # Continuously try to download after private/deleted videos are found
        # This block gives the downloader all the curated videos and skips/reports deleted videos by filtering their exceptions
        while True:
            # Download from curated list then exit the optimistic loop
            try:
                urls = [video.url() for video in not_downloaded]
                with YoutubeDL(settings) as ydl:
                    ydl.download(urls)
                break

            # Special handling for private/deleted videos which are archived, if not we raise again
            except DownloadError as exception:
                new_not_downloaded = self._dl_exception_handle(
                    not_downloaded, exception
                )
                if new_not_downloaded is not None:
                    not_downloaded = new_not_downloaded

    def _dl_exception_handle(
        self, not_downloaded: list[Video], exception: DownloadError
    ) -> Optional[list[Video]]:
        """Handle for failed downloads if there's a special private/deleted video"""
        # Set new list for not downloaded to return later
        new_not_downloaded = None

        # Video is privated or deleted
        if (
            "Private video" in exception.msg
            or "This video has been removed by the uploader" in exception.msg
        ):
            # Skip video from curated and get it as a return
            new_not_downloaded, video = _skip_video(not_downloaded, "deleted")

            # If this is a new occurrence then set it & report
            # This will only happen if its deleted after getting metadata, like in a dry run
            if video.deleted.current() == False:
                self.reporter.deleted.append(video)
                video.deleted.update(None, True)

        # User hasn't got ffmpeg installed and youtube hasn't got format 22
        # NOTE: see #55 <https://github.com/Owez/yark/issues/55> to learn more
        # NOTE: sadly yt-dlp doesn't let us access yt_dlp.utils.ContentTooShortError so we check msg
        elif " bytes, expected " in exception.msg:
            # Skip video from curated
            new_not_downloaded, _ = _skip_video(
                not_downloaded,
                "no format found; please download ffmpeg!",
                True,
            )

        # Nevermind, normal exception
        else:
            raise exception

        # Return
        return new_not_downloaded

    def _curate(self, config: Config) -> list[Video]:
        """Curate videos which aren't downloaded and return their urls"""

        def curate_list(videos: Videos, maximum: Optional[int]) -> list[Video]:
            """Curates the videos inside of the provided `videos` list to it's local maximum"""
            # Make a list for the videos
            found_videos = []

            # Add all undownloaded videos because there's no maximum
            if maximum is None:
                found_videos = list(
                    [video for video in videos.inner.values() if not video.downloaded()]
                )

            # Cut available videos to maximum if present for deterministic getting
            else:
                # Fix the maximum to the length so we don't try to get more than there is
                fixed_maximum = min(max(len(videos.inner) - 1, 0), maximum)

                # Set the available videos to this fixed maximum
                values = list(videos.inner.values())
                for ind in range(fixed_maximum):
                    # Get video
                    video = values[ind]

                    # Save video if it's not been downloaded yet
                    if not video.downloaded():
                        found_videos.append(video)

            # Return
            return found_videos

        # Curate
        not_downloaded = []
        not_downloaded.extend(curate_list(self.videos, config.max_videos))
        not_downloaded.extend(curate_list(self.livestreams, config.max_livestreams))
        not_downloaded.extend(curate_list(self.shorts, config.max_shorts))

        # Return
        return not_downloaded

    def commit(self, backup: bool = False) -> None:
        """Commits (saves) archive to path; do this once you've finished all of your transactions"""
        # Save backup if explicitly wanted
        if backup:
            self._backup()

        # Directories
        print(f"Committing {self} to file..")
        paths = [self.path, self.path / "images", self.path / "videos"]
        for path in paths:
            if not path.exists():
                path.mkdir()

        # Config
        with open(self.path / "yark.json", "w+") as file:
            json.dump(self._to_archive_o(), file)

    def _report_deleted(self, videos: Videos) -> None:
        """Goes through a video category to report & save those which where not marked in the metadata as deleted if they're not already known to be deleted"""
        for video in videos.inner.values():
            if video.deleted.current() == False and not video.known_not_deleted:
                self.reporter.deleted.append(video)
                video.deleted.update(None, True)

    def _clean_parts(self) -> None:
        """Cleans old temporary `.part` files which where stopped during download if present"""
        # Make a bucket for found files
        deletion_bucket: list[Path] = []

        # Scan through and find part files
        videos = self.path / "videos"
        deletion_bucket.extend([file for file in videos.glob("*.part")])
        deletion_bucket.extend([file for file in videos.glob("*.ytdl")])

        # Print and delete if there are part files present
        if len(deletion_bucket) != 0:
            print("Cleaning out previous temporary files..")
            for file in deletion_bucket:
                file.unlink()

    def _backup(self) -> None:
        """Creates a backup of the existing `yark.json` file in path as `yark.bak` with added comments"""
        # Get current archive path
        ARCHIVE_PATH = self.path / "yark.json"

        # Skip backing up if the archive doesn't exist
        if not ARCHIVE_PATH.exists():
            return

        # Open original archive to copy
        with open(self.path / "yark.json", "r") as file_archive:
            # Add comment information to backup file
            save = f"// Backup of a Yark archive, dated {datetime.utcnow().isoformat()}\n// Remove these comments and rename to 'yark.json' to restore\n{file_archive.read()}"

            # Save new information into a new backup
            with open(self.path / "yark.bak", "w+") as file_backup:
                file_backup.write(save)

    @staticmethod
    def _from_archive_o(encoded: dict[str, Any], path: Path) -> Archive:
        """Decodes object dict from archive which is being loaded back up"""

        # Initiate archive
        archive = Archive(path, encoded["url"], encoded["version"])

        # Decode id & body style comment authors
        # NOTE: needed above video decoding for comments
        for id in encoded["comment_authors"].keys():
            archive.comment_authors[id] = CommentAuthor._from_archive_ib(
                archive, id, encoded["comment_authors"][id]
            )

        # Load up videos/livestreams/shorts
        archive.videos = Videos._from_archive_o(archive, encoded["videos"])
        archive.livestreams = Videos._from_archive_o(archive, encoded["livestreams"])
        archive.shorts = Videos._from_archive_o(archive, encoded["shorts"])

        # Return
        return archive

    def _to_archive_o(self) -> dict[str, Any]:
        """Converts all archive data to a object dict to commit"""
        # Encode comment authors
        comment_authors = {}
        for id in self.comment_authors.keys():
            comment_authors[id] = self.comment_authors[id]._to_archive_b()

        # Basics
        payload = {
            "version": self.version,
            "url": self.url,
            "videos": self.videos._to_archive_o(),
            "livestreams": self.livestreams._to_archive_o(),
            "shorts": self.shorts._to_archive_o(),
            "comment_authors": comment_authors,
        }

        # Return
        return payload

    def __repr__(self) -> str:
        return self.path.name


def _log_download_count(count: int) -> None:
    """Tells user that `count` number of videos have been downloaded"""
    fmt_num = "a new video" if count == 1 else f"{count} new videos"
    print(f"Downloading {fmt_num}..")


def _skip_video(
    videos: list[Video],
    reason: str,
    warning: bool = False,
) -> tuple[list[Video], Video]:
    """Skips first undownloaded video in `videos`, make sure there's at least one to skip otherwise an exception will be thrown"""
    # Find fist undownloaded video
    for ind, video in enumerate(videos):
        if not video.downloaded():
            # Tell the user we're skipping over it
            if warning:
                print(
                    Fore.YELLOW + f"  • Skipping {video.id} ({reason})" + Fore.RESET,
                    file=sys.stderr,
                )
            else:
                print(
                    Style.DIM + f"  • Skipping {video.id} ({reason})" + Style.NORMAL,
                )

            # Set videos to skip over this one
            videos = videos[ind + 1 :]

            # Return the corrected list and the video found
            return videos, video

    # Shouldn't happen, see docs
    raise Exception(
        "We expected to skip a video and return it but nothing to skip was found"
    )


def _err_dl(name: str, exception: DownloadError, retrying: bool) -> None:
    """Prints errors to stdout depending on what kind of download error occurred"""
    # Default message
    msg = f"Unknown error whilst downloading {name}, details below:\n{exception}"

    # Types of errors
    ERRORS = [
        "<urlopen error [Errno 8] nodename nor servname provided, or not known>",
        "500",
        "Got error: The read operation timed out",
        "No such file or directory",
        "HTTP Error 404: Not Found",
        "<urlopen error timed out>",
    ]

    # Download errors
    if type(exception) == DownloadError:
        # Server connection
        if ERRORS[0] in exception.msg:
            msg = "Issue connecting with YouTube's servers"

        # Server fault
        elif ERRORS[1] in exception.msg:
            msg = "Fault with YouTube's servers"

        # Timeout
        elif ERRORS[2] in exception.msg:
            msg = "Timed out trying to download video"

        # Video deleted whilst downloading
        elif ERRORS[3] in exception.msg:
            msg = "Video deleted whilst downloading"

        # Target not found, might need to retry with alternative route
        elif ERRORS[4] in exception.msg:
            msg = "Couldn't find target by it's id"

        # Random timeout; not sure if its user-end or youtube-end
        elif ERRORS[5] in exception.msg:
            msg = "Timed out trying to reach YouTube"

    # Print error
    suffix = ", retrying in a few seconds.." if retrying else ""
    print(
        Fore.YELLOW + "  • " + msg + suffix.ljust(40) + Fore.RESET,
        file=sys.stderr,
    )

    # Wait if retrying, exit if failed
    if retrying:
        time.sleep(5)
    else:
        _err_msg(f"  • Sorry, failed to download {name}", True)
        sys.exit(1)


def _progress_spinner(msg: str, future: Future[Any]) -> None:
    """Shows a progress spinner displaying `msg` after 2 seconds until future is finished"""
    # Print loading progress at the starts without loading indicator so theres always a print
    print(msg, end="\r")

    # Start spinning
    with PieSpinner(f"{msg} ") as bar:
        # Don't show bar for 2 seconds but check if future is done
        no_bar_time = time.time() + 2
        while time.time() < no_bar_time:
            if future.done():
                return
            time.sleep(0.25)

        # Show loading spinner
        while not future.done():
            bar.next()
            time.sleep(0.075)
