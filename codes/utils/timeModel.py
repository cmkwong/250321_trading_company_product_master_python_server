from datetime import datetime
from dateutil.relativedelta import relativedelta
import pytz

def get_time_str(format='%Y%m%d%H%M%S'):
    now = datetime.now()
    return now.strftime(format)

# split into month between start and end datetime
def split_month_period(start, end, format="%Y-%m-%d %H:%M:%S"):
    """
    :param start: datetime / str
    :param end: datetime / str
    :return: list [ [start, end], ... ]
    """
    if isinstance(start, str): start = datetime.strptime(start, format)
    if isinstance(end, str): end = datetime.strptime(end, format)

    time_periods = []
    # get how many month of difference between start and end
    diff_months = relativedelta(end, start).months + relativedelta(end, start).years * 12
    for month in range(diff_months + 1):
        _s = start + relativedelta(months=month)
        _e = _s + relativedelta(months=1) - relativedelta(seconds=1)
        time_periods.append([_s, _e])

    # reassign the start and end in whole list
    time_periods[0][0] = start
    time_periods[-1][1] = end

    return time_periods

def getYearMonth_byLast(last: int = 1):
    """
    getting the year and month by input how many last
    """
    now = datetime.now()
    if last >= 0:
        last_now = now + relativedelta(months=last)
    else:
        last_now = now - relativedelta(months=-last)
    return last_now.year, last_now.month

def get_start_end(year=None, month=None, format="%Y-%m-%d %H:%M:%S", return_strings=False, tz=None):
    """
    Get start and end datetime of a month with improved validation and flexibility
    
    Args:
        year (int, optional): Year. If None, uses current year. Defaults to None.
        month (int, optional): Month (1-12). If None, uses current month. Defaults to None.
        format (str, optional): Datetime format string. Defaults to "%Y-%m-%d %H:%M:%S".
        return_strings (bool, optional): Return formatted strings instead of datetime objects. Defaults to False.
        tz (str, optional): Timezone string (e.g. 'Asia/Hong_Kong'). Defaults to None.
        
    Returns:
        tuple: (start_datetime, end_datetime) or (start_str, end_str) if return_strings=True
    
    Raises:
        ValueError: If year or month are invalid
    """
    now = datetime.now()
    
    # Validate and set year
    if year is None:
        year = now.year
    elif not isinstance(year, int) or year < 1:
        raise ValueError("Year must be a positive integer")
        
    # Validate and set month
    if month is None:
        month = now.month
    elif not isinstance(month, int) or month < 1 or month > 12:
        raise ValueError("Month must be an integer between 1 and 12")
    
    # Create start datetime
    start = datetime(year, month, 1, 0, 0, 0)
    end = start + relativedelta(months=1) - relativedelta(seconds=1)
    
    # Apply timezone if specified
    if tz:
        try:
            tz_obj = pytz.timezone(tz)
            start = tz_obj.localize(start)
            end = tz_obj.localize(end)
        except ImportError:
            raise ImportError("pytz package required for timezone support")
        except pytz.UnknownTimeZoneError:
            raise ValueError(f"Unknown timezone: {tz}")
    
    if return_strings:
        return start.strftime(format), end.strftime(format)
    return start, end
