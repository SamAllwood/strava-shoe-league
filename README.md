# Strava Shoe League

This project is a Python application that integrates with the Strava API to retrieve athlete data, process activities, generate a shoe league table and present marathon races completed. Just for fun.

## Project Structure

```
strava-shoe-league
├── Strava_shoe_league.py        # Main script for the application
├── strava_tools.py              # Utility functions for Strava API interactions
├── requirements.txt             # Project dependencies
├── .gitignore                   # Files and directories to ignore in Git
├── README.md                    # Documentation for the project
├── data
│   └── shoe_league_table.csv    # CSV file for shoe league data
│   └── activities_19000711.json # JSON file containing strava data for Sam Allwood as an example. Saves downloading all the data every time you run app.
└── tests
    └── test_shoe_league.py      # Unit tests for the application
```

## Setup Instructions

1. **Clone the Repository**:
   ```bash
   git clone <repository-url>
   cd strava-shoe-league
   ```

2. **Install Dependencies**:
   It is recommended to use a virtual environment. You can create one using `venv` or `conda`. After activating your environment, install the required packages:
   ```bash
   pip install -r requirements.txt
   ```

3. **Configure Strava API**:
   You need to set up your Strava API credentials. Create a `.env` file or set environment variables for the following:
   - `STRAVA_CLIENT_ID`
   - `STRAVA_CLIENT_SECRET`
   - `STRAVA_REDIRECT_URI`

## Usage

To run the application, execute the following command:
```bash
python Strava_shoe_league.py
```

Follow the prompts to connect to your Strava account and retrieve your activity data.

## Testing

To run the unit tests, use the following command:
```bash
pytest tests/test_shoe_league.py
```

## License

This project is licensed under the MIT License. See the LICENSE file for more details.