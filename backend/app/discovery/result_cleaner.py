class ResultCleaner:

    def remove_duplicates(
        self,
        leaders: list,
    ):

        unique = {}
        cleaned = []

        for leader in leaders:

            name = (
                leader.get("name", "")
                .strip()
                .lower()
            )

            title = (
                leader.get("title", "")
                .strip()
                .lower()
            )

            text = (
                leader.get("text", "")
                .strip()
                .lower()
            )

            key = (name, title)

            if not name:
                key = (title, text[:100])

            if key in unique:
                continue

            unique[key] = True

            cleaned.append(leader)

        return cleaned